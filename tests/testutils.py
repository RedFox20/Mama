import os
import re
import shutil
import subprocess
import sys
import threading
from types import SimpleNamespace
from typing import Iterable, Optional
from unittest.mock import Mock

import mama
import pytest

from mama.platforms.platform import Platform
from mama.platforms.linux import Linux

_ANSI = re.compile(r'\x1b\[[0-9;]*[A-Za-z]')  # SGR colours + cursor moves
def strip_ansi(s: str) -> str: return _ANSI.sub('', s)


class FakeBuildTarget:
    """Base for the runner-test target fakes: the build-weight stubs the parallel runners call on
    every dep (configure/build phase bodies and event recording stay specialised per test)."""
    _build_jobs = None
    def _has_custom_build(self): return False
    def _reserved_cores(self): return 4


class FakeUnifiedTarget(FakeBuildTarget):
    """Target half of the execute_unified fakes: every phase appends (tag, dep-name) to a shared list."""
    def __init__(self, dep, ev, lock):
        self.dep = dep; self._ev = ev; self._lock = lock; self._out_sink = None
    def _rec(self, tag):
        with self._lock: self._ev.append((tag, self.dep.name))
    def configure_phase(self, out=None): self._rec('cfg')
    def build_phase(self, out=None): self._rec('bld')
    def clean(self): self._rec('clean')
    def _execute_deploy_tasks(self): pass
    def _execute_run_tasks(self): pass


class FakeUnifiedDep:
    """Dep half of the execute_unified fakes: load() discovers `child_specs` (name, grandchild-specs)
    on the spot, so the scheduler grows the graph the way a real clone does. Pass `shared_children`
    instead to hand two parents the SAME instance and form a diamond."""
    def __init__(self, name, config, ev, lock, child_specs=(), shared_children=None):
        self.name = name; self.config = config; self._ev = ev; self._lock = lock
        self.phase_times = {}; self.should_rebuild = False; self.from_artifactory = False; self.nothing_to_build = False
        self._child_specs = child_specs; self._shared = shared_children
        self._children = []; self.already_executed = False
        self.is_root = False; self.load_action = 'check'; self.target = FakeUnifiedTarget(self, ev, lock)
    def load(self):
        with self._lock: self._ev.append(('load', self.name))
        self._children = self._shared if self._shared is not None else \
            [FakeUnifiedDep(n, self.config, self._ev, self._lock, cs) for n, cs in self._child_specs]
    def get_children(self): return self._children
    def after_load(self): pass
    def clean(self): self.target.clean()
    def create_build_dir_if_needed(self): pass
    def is_root_or_config_target(self): return False
    def is_real_clone(self): return False  # load label resolves to 'clone'


def make_unified_config(**overrides):
    """The BuildConfig fields execute_unified and its display/scheduler touch."""
    cfg = SimpleNamespace(jobs=2, parallel_max=8, verbose=False, test=False, update_stats=Mock(),
                          workspaces_root=None, buildstats=False, msvc=False, clang=False, gcc=True,
                          rebuild=False, update=False, clean=False, target=None, name=lambda: 'linux')
    for k, v in overrides.items(): setattr(cfg, k, v)
    return cfg


def make_mock_config(tmp_path, **overrides):
    """Mock BuildConfig pre-populated with the defaults every shim/probe/dep
    unit test needs. Pass kwargs to override specific fields per test."""
    cfg = Mock()
    cfg.artifactory_ftp = 'ftp.example.com'
    cfg.workspaces_root = str(tmp_path)
    cfg.global_workspace = False
    cfg.platform_build_dir_name.return_value = 'linux'
    cfg.verbose = False
    cfg.print = False
    cfg.loaded_dependencies = {}
    cfg.dep_registry_lock = threading.Lock()  # real lock so add_child works under the mock config
    cfg.target_matches.return_value = False
    cfg.force_artifactory = False
    cfg.disable_artifactory = False
    cfg.is_network_available.return_value = True
    cfg.unshallow = False
    cfg.git_url_override = None
    cfg.update_stats = Mock()
    # commands off by default - tests opt in explicitly
    cfg.build = False
    cfg.update = False
    cfg.clean = False
    cfg.rebuild = False
    cfg.reclone = False
    cfg.run_cmake_configure = False
    cfg.target = None
    cfg.cmake_toolchain_file = ''  # a toolchain-file build takes a different compiler path
    cfg.clean_only.return_value = False  # Mock methods are truthy by default
    cfg.list = False
    # platform: a REAL Linux instance, so option builders get real strings instead of Mocks
    cfg.msvc = False
    cfg.linux = True
    cfg.macos = False
    cfg.ios = None
    cfg.android = None
    cfg.raspi = None
    cfg.oclea = None
    cfg.xilinx = None
    cfg.mips = None
    cfg.imx8mp = None
    cfg.yocto_linux = None
    cfg.clang = False
    cfg.gcc = True
    cfg.clang_stdlib = 'libc++'
    cfg.clang_tidy_path = None
    cfg.fortran = ''
    cfg.flags = None
    cfg.coverage = None
    cfg.with_tests = False
    cfg.buildstats = False
    cfg.debug = False
    cfg.prefer_ninja = False
    cfg.ninja_path = ''
    cfg.cmake_command = 'cmake'
    # artifactory_archive_name uses these
    cfg.get_distro_info.return_value = ('ubuntu', 22, 4)
    cfg.compiler_version.return_value = 'gcc11.3'
    cfg.arch = 'x64'
    cfg.release = True
    cfg.sanitize = None
    cfg.sanitizer_suffix.return_value = ''
    for k, v in overrides.items(): setattr(cfg, k, v)
    if not isinstance(getattr(cfg, 'platform', None), Platform):
        set_mock_platform(cfg, Linux)  # after overrides, so a test can pass its own platform=
    return cfg


def set_mock_platform(cfg, platform_class):
    """Install a REAL platform instance on a mock config and refresh the mamafile-facing flags, the
    way BuildConfig.set_platform_class() does. Mock() would answer every option builder with a Mock."""
    from mama.build_config import BuildConfig
    cfg.platform = platform_class(cfg)
    BuildConfig._update_platform_flags(cfg)
    return cfg.platform


def platform_config(platform_class, arch=None, **overrides):
    """A REAL BuildConfig on `platform_class`, the way a `mama build <platform>` run produces one."""
    from mama.build_config import BuildConfig
    cfg = BuildConfig([])
    cfg.set_platform_class(platform_class)
    cfg.arch = arch or platform_class.default_arch or cfg.arch
    for k, v in overrides.items(): setattr(cfg, k, v)
    return cfg


MAMA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'mama')
CMAKE_OPTIONS = 'buildsys/cmake/options.py'  # the ONE module allowed to format a cmake option


def grep_mama_sources(needles, skip=()) -> list:
    """Every `<rel path>:<line no>` under mama/ whose source line holds any of `needles`, skipping the
    files in `skip`. Backs the layering tests: one module formats a build-system option, never eleven."""
    hits = []
    for root, _, files in os.walk(MAMA_DIR):
        for f in files:
            if not f.endswith('.py'): continue
            path = os.path.join(root, f)
            rel = os.path.relpath(path, MAMA_DIR).replace('\\', '/')
            if rel in skip: continue
            with open(path, encoding='utf-8') as handle:
                hits += [f'{rel}:{n}' for n, line in enumerate(handle, 1) if any(x in line for x in needles)]
    return hits


def make_mock_dep(tmp_path, name='libfoo', url='https://example.com/libfoo.git',
                  branch='main', tag='', mamafile=None, **config_overrides):
    """Real BuildDependency wired to a mock BuildConfig + a Git dep_source.
    Used by shim/probe/load-integration/noart tests that need real
    is_artifactory_shim() / shim-marker semantics on disk."""
    from mama.build_dependency import BuildDependency
    from mama.types.git import Git
    config = make_mock_config(tmp_path, **config_overrides)
    git = Git(name=name, url=url, branch=branch, tag=tag, mamafile=mamafile, shallow=True, args=[])
    dep = BuildDependency(parent=None, config=config, workspace='packages', dep_source=git)
    dep.is_root = False  # tests rarely have a real parent chain
    dep.create_build_dir_if_needed()
    return dep


def make_mock_local_dep(tmp_path, src_dir, name='libfoo', always_build=False, **config_overrides):
    """Real BuildDependency wired to a mock BuildConfig + a LocalSource pointing at an existing
    on-disk `src_dir`. build_dir is materialised so src_status round-trips."""
    from mama.build_dependency import BuildDependency
    from mama.types.local_source import LocalSource
    config = make_mock_config(tmp_path, **config_overrides)
    src = LocalSource(name=name, rel_path=str(src_dir), mamafile=None, always_build=always_build, args=[])
    dep = BuildDependency(parent=None, config=config, workspace='packages', dep_source=src)
    dep.is_root = False
    dep._update_dep_name_and_dirs(name)
    dep.create_build_dir_if_needed()
    return dep


def make_mock_shim_dep(tmp_path, stored_hash='abc1234', write_papa_txt=False, **config_overrides):
    """make_mock_dep + a shim marker already written. Optionally seeds papa.txt
    so artifactory_load_target can parse it (for noart cache-hit tests)."""
    dep = make_mock_dep(tmp_path, **config_overrides)
    dep.write_shim_marker(archive_name=f'libfoo-linux-22-gcc11.3-x64-release-{stored_hash}',
                          commit_hash=stored_hash)
    if write_papa_txt:
        (tmp_path / 'packages/libfoo/linux/papa.txt').write_text('p libfoo\nv 1.0\n')
    return dep


def make_configured_target(tmp_path, compiler=('/usr/bin/gcc', '/usr/bin/g++', '13.3'), **config_overrides):
    """A real BuildTarget on a fresh pkg/ dir with the preferred compiler paths mocked - the shared starting
    point for cmake configure tests. Returns (target, dep)."""
    sub = tmp_path / 'pkg'; sub.mkdir(exist_ok=True)
    dep = make_mock_local_dep(tmp_path, src_dir=sub, jobs=8, coverage=False, clang_tidy=False, **config_overrides)
    dep.config.get_preferred_compiler_paths.return_value = compiler
    return dep.target, dep


def run_config_capturing(target, dep):
    """Drive cmake configure.run_config with the cmake call + seed coordinator stubbed. Returns the
    configure command lines it would have run, so a test can assert on the flags without a real cmake."""
    from unittest.mock import patch
    from mama.buildsys.cmake import configure as cmake_configure
    cmds = []
    with patch('mama.buildsys.cmake.configure._rerunnable_cmake_conf', side_effect=lambda cmd, *a, **k: cmds.append(cmd)), \
         patch('mama.buildsys.cmake.configure.compute_env', return_value={}), \
         patch('mama.buildsys.cmake.configure._seed_coordinator') as coord, \
         patch.object(dep, 'get_enabled_sanitizers', return_value=''):
        coord.return_value.prepare.return_value = 'none'
        coord.return_value.status.return_value = ('fp', False)
        cmake_configure.run_config(target)
    return cmds


def write_cmake_cache(build_dir, text):
    """Write a raw CMakeCache.txt into build_dir (created if missing)."""
    os.makedirs(build_dir, exist_ok=True)
    with open(os.path.join(build_dir, 'CMakeCache.txt'), 'w', encoding='utf-8') as f: f.write(text)


def write_build_file(build_dir, name='build.ninja'):
    """Write the generator's build file so is_cmake_cache_valid() sees a completed configure."""
    with open(os.path.join(build_dir, name), 'w', encoding='utf-8') as f: f.write('# generated\n')


def init(caller_file: str = '', clean_dirs: Optional[Iterable[str]] = None):
    # Needed for mama commands to perform work in the correct directory
    if caller_file:
        os.chdir(os.path.dirname(os.path.abspath(caller_file)))

    if clean_dirs is None:
        clean_dirs = ()

    for d in clean_dirs:
        rmdir(d)

def shell_exec(cmd: str, exit_on_fail: bool = True, echo: bool = True) -> int:
    if echo: print(f'exec: {cmd}')
    result = subprocess.run(cmd, shell=True)
    if result.returncode != 0 and exit_on_fail:
        pytest.fail(f'exec failed: code: {result.returncode} {cmd}')
    return result.returncode

def mama_exec(args: list[str], exit_on_fail: bool = True, echo: bool = True) -> int:
    """Calls mama.mamabuild() directly instead of shelling out to the mama CLI."""
    if echo: print(f'mama: {" ".join(args)}')
    try:
        mama.mamabuild(args, source_dir=os.getcwd())
        return 0
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 1
        if code != 0 and exit_on_fail:
            pytest.fail(f'mama failed: code: {code} args: {args}')
        return code
    except Exception as e:
        if exit_on_fail:
            pytest.fail(f'mama failed: {e} args: {args}')
        return 1

def file_contains(filepath: str, text: str) -> bool:
    with open(filepath, 'r') as f:
        content = f.read()
    return text in content

def file_exists(filepath: str) -> bool:
    return os.path.isfile(filepath)

def is_windows() -> bool:
    return os.name == 'nt'

def is_linux() -> bool:
    return os.name == 'posix' and sys.platform != 'darwin'

def is_macos() -> bool:
    return sys.platform == 'darwin'

def executable_extension() -> str:
    if is_windows():
        return '.exe'

    return ''

def static_library_extension() -> str:
    if is_windows():
        return '.lib'
    else:
        return '.a'

def dynamic_library_extension() -> str:
    if is_windows():
        return '.dll'
    elif is_macos():
        return '.dylib'
    else:
        return '.so'

# Excludes for example android
def native_platform_name() -> str:
    if is_windows():
        return 'windows'
    elif is_linux():
        return 'linux'
    elif is_macos():
        return 'macos'
    else:
        raise Exception("Unsupported platform")

def onerror(func, path, _):
    import stat
    if not os.access(path, os.W_OK):
        os.chmod(path, stat.S_IWUSR)
        func(path)

def rmdir(path: str):
    if os.path.exists(path):
        shutil.rmtree(path, onerror=onerror)