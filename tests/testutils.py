import contextlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from functools import lru_cache
from types import SimpleNamespace
from unittest.mock import Mock

import mama
import pytest

from mama.build_config import DeployStats
from mama.platforms.platform import Platform
from mama.platforms.linux import Linux
from mama.utils.fileio import write_text_to
from mama.utils.paths import normalized_path, path_join
from mama.utils.sub_process import execute_piped

_ANSI = re.compile(r'\x1b\[[0-9;]*[A-Za-z]')  # SGR colors + cursor moves
def strip_ansi(s: str) -> str: return _ANSI.sub('', s)

def summary_lines(text: str) -> list:
    """The lines a non-tty BuildDisplay committed, without the `>` line that opens each phase."""
    return [l for l in strip_ansi(text).splitlines() if l.strip() and not l.startswith('>')]


class FakeBuildTarget:
    """Base for the runner-test target fakes: the build-weight stubs the parallel runners call on
    every dep (configure/build phase bodies and event recording stay specialized per test)."""
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
    at load time, so the scheduler grows the graph the way a real clone does. Pass `shared_children`
    instead to hand two parents the SAME instance and form a diamond."""
    dep_source = SimpleNamespace(is_src=False)  # a git dep, so the opening load label resolves to 'clone'
    def __init__(self, name, config, ev, lock, child_specs=(), shared_children=None):
        self.name = name; self.config = config; self._ev = ev; self._lock = lock
        self.phase_times = {}; self.should_rebuild = False; self.from_artifactory = False; self.nothing_to_build = False
        self._child_specs = child_specs; self._shared = shared_children
        self._children = []; self.already_executed = False
        self.is_root = False; self.load_action = 'check'; self.artifactory_archive = ''
        self.build_dir = ''  # no cache on disk, so the mixed build-type check finds nothing
        self.target = FakeUnifiedTarget(self, ev, lock)
    def load(self):
        with self._lock: self._ev.append(('load', self.name))
        self._children = self._shared if self._shared is not None else \
            [FakeUnifiedDep(n, self.config, self._ev, self._lock, cs) for n, cs in self._child_specs]
    def get_children(self): return self._children
    def after_load(self): pass
    def clean(self): self.target.clean()
    def create_build_dir_if_needed(self): pass
    def is_root_or_config_target(self): return False
    def is_real_clone(self): return False


class FakeWalkDep:
    """Dep for the load_dependency_chain walk tests. load() records its name and returns the children the
    test declared, so the log holds the walk order."""
    dep_source = SimpleNamespace(is_src=False)  # a git dep, so the opening load label resolves to 'clone'
    def __init__(self, name, config, log, children=(), loaded=False, on_load=None):
        self.name = name; self.config = config; self._log = log; self._children = list(children)
        self.already_loaded = loaded; self.should_rebuild = False; self.is_root = False
        self.load_action = 'check'; self.artifactory_archive = ''; self.phase_times = {}
        self.load_deferred = False
        self._on_load = on_load  # what this dep prints while it loads
    def revive_deferred_load(self):
        self.load_deferred = False; self.already_loaded = False
    def load(self):
        self._log.append(self.name)
        if self._on_load: self._on_load()
        self.already_loaded = True
        return self.should_rebuild
    def get_children(self): return self._children
    def after_load(self): pass
    def is_real_clone(self): return False


def make_walk_config(**overrides):
    """The BuildConfig fields load_dependency_chain touches. Serial by default, so a test reads one order."""
    cfg = SimpleNamespace(serial_load=True, parallel_load=False, parallel_max=4, print=False,
                          verbose=False, update_stats=Mock())
    cfg.update_stats.summary_line.return_value = ''
    for k, v in overrides.items(): setattr(cfg, k, v)
    return cfg


def make_unified_config(**overrides):
    """The BuildConfig fields execute_unified and its display/scheduler touch."""
    cfg = SimpleNamespace(jobs=2, parallel_max=8, verbose=False, test=False, update_stats=Mock(), print=False, debug=False,
                          deploy_stats=DeployStats(), unpublish='',
                          workspaces_root=None, buildstats=False, msvc=False, clang=False, gcc=True,
                          rebuild=False, update=False, clean=False, target=None, name=lambda: 'linux')
    for k, v in overrides.items(): setattr(cfg, k, v)
    return cfg


def stub_loaders(grow_tree):
    """Every loader a mamabuild run can enter, patched to `grow_tree`. A targeted run enters
    load_path_to_target and an untargeted one load_dependency_chain. Stage two calls the second
    through its own module, so that name needs its own patch. Returns an ExitStack for a `with`."""
    from contextlib import ExitStack
    from unittest.mock import patch
    stack = ExitStack()
    stack.enter_context(patch('mama.main.load_dependency_chain', side_effect=lambda r, display=None: grow_tree(r)))
    stack.enter_context(patch('mama.main.load_path_to_target', side_effect=grow_tree))
    stack.enter_context(patch('mama.dependency_chain.load_dependency_chain'))
    return stack


@contextlib.contextmanager
def stub_runners(*also, **side_effects):
    """Every task runner a mamabuild run can reach, plus the banner, patched out. `also` names more
    mama.main attributes to patch, and a keyword gives one of them a side effect. Yields the mocks by
    name, so a test reads back which runner the run picked."""
    from unittest.mock import patch
    runners = ('execute_task_chain', 'execute_task_chain_parallel', 'execute_unified', 'print_build_banner')
    with contextlib.ExitStack() as stack:
        def stub(name):
            effect = side_effects.get(name)
            target = f'mama.main.{name}'
            return stack.enter_context(patch(target, side_effect=effect) if effect else patch(target))
        yield {name: stub(name) for name in dict.fromkeys((*runners, *also, *side_effects))}


def make_project_dir(tmp_path, project='dummy') -> str:
    """Write the CMakeLists.txt that mamabuild refuses to start without, and return the dir it made.
    Pass the result straight to mamabuild(source_dir=...)."""
    write_text_to(path_join(str(tmp_path), 'CMakeLists.txt'), f'project({project})\n')
    return str(tmp_path)


def make_tree_dep(name, children=(), usable=True, deferred=False, free=False):
    """SimpleNamespace dep for the scoping passes of a whole mamabuild run. It answers the walk, the
    mark, the revive and the post-chain report that every public command reaches."""
    target = Mock(build_products=[], args='')
    target.name = name  # Mock(name=..) names the mock itself, not the attribute
    d = SimpleNamespace(name=name, should_rebuild=False, load_deferred=deferred, revived=False, already_loaded=False,
                        children=list(children), target=target, from_artifactory=False, artifactory_archive='')
    d.get_children = lambda d=d: d.children
    d.has_usable_artifacts = lambda usable=usable: usable
    d.load_is_free = lambda free=free: free
    d.get_enabled_coverage = lambda: False
    def revive(d=d): d.load_deferred = False; d.revived = True; d.already_loaded = False
    d.revive_deferred_load = revive
    return d


def make_mock_config(tmp_path, **overrides):
    """Mock BuildConfig pre-populated with the defaults every shim/probe/dep
    unit test needs. Pass kwargs to override specific fields per test."""
    cfg = Mock()
    cfg.artifactory_ftp = 'ftp.example.com'
    cfg.workspaces_root = str(tmp_path)
    cfg.global_workspace = False
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
    cfg.deploy_stats = DeployStats()
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
    cfg.unpublish = ''  # a Mock attribute is truthy, and a truthy one would unpublish in every test
    cfg.unpublish_keep = None
    cfg.assume_yes = False
    # platform: a REAL Linux instance, so option builders get real strings instead of Mocks
    # a Mock attribute is truthy, and both of these would then read as an explicit compiler choice
    cfg.compiler_cmd = cfg.compiler_from_args = False
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
    cfg.ninja_version.return_value = ''  # the generated mama.cmake writes this number verbatim
    cfg.cmake_command = 'cmake'
    # artifactory_archive_name and the papa `O` record use these
    cfg.get_distro_info.return_value = ('ubuntu', 22, 4)
    cfg.compiler_version.return_value = 'gcc11.3'
    cfg.name.return_value = 'linux'
    cfg.arch = 'x64'
    cfg.release = True
    cfg.sanitize = None
    cfg.target_march = {}  # a Mock dict would answer .get() with a truthy Mock and rename every build dir
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


def platform_target(tmp_path, platform_class, arch=None, **overrides):
    """A configure-ready (target, dep) on `platform_class` and `arch`, for the option-builder tests."""
    target, dep = make_configured_target(tmp_path, **overrides)
    dep.config.arch = arch or platform_class.default_arch or 'x64'
    dep.config.cmake_toolchain_file = ''
    set_mock_platform(dep.config, platform_class)
    return target, dep


def platform_cxx_flags(tmp_path, platform_class, arch=None, **overrides):
    """The C++ flags `platform_class` puts on the cmake command line for `arch`."""
    from mama.buildsys.cmake import configure as cc
    target, _ = platform_target(tmp_path, platform_class, arch, **overrides)
    cc._default_options(target)
    return target.cmake_cxxflags


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAMA_DIR = os.path.join(REPO_ROOT, 'mama')
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


def make_package_target(tmp_path, package=None, exports=None, dep_attrs=None, **config):
    """A BuildTarget wired to a mock dep, for the packaging tests.
    package: the mamafile hook, or None for a Mock that only records that it ran
    exports: the (includes, libs, syslibs, assets[, modules]) papa.txt would have loaded
    dep_attrs: BuildDependency fields this test needs, eg from_artifactory"""
    from mama.build_target import BuildTarget
    dep = make_mock_dep(tmp_path, **config)
    dep.nothing_to_build = False; dep.from_artifactory = False; dep.should_rebuild = False
    for k, v in (dep_attrs or {}).items(): setattr(dep, k, v)
    cls = type('mamafile', (BuildTarget,), {'package': package}) if package else BuildTarget
    target = cls(name=dep.name, config=dep.config, dep=dep, args=[])
    dep.target = target
    if not package: target.package = Mock()
    # a caller that names no modules still gets the full export tuple, so the categories stay optional
    if exports: target._set_exports(tuple(list(x) for x in exports) + ([],) * (5 - len(exports)))
    return target


def make_mock_dep(tmp_path, name='libfoo', url='https://example.com/libfoo.git', branch='main', tag='',
                  mamafile=None, commit='abc1234', **config_overrides):
    """Real BuildDependency wired to a mock BuildConfig + a Git dep_source.
    Used by shim/probe/load-integration/noart tests that need real
    is_artifactory_shim() / shim-marker semantics on disk.
    commit: pre-resolved commit hash. Without it, every path that names a package runs a real
            `git ls-remote https://example.com/...`, which reaches the network. Pass None for a test
            that drives the resolution itself."""
    from mama.build_dependency import BuildDependency
    from mama.types.git import Git
    config = make_mock_config(tmp_path, **config_overrides)
    git = Git(name=name, url=url, branch=branch, tag=tag, mamafile=mamafile, shallow=True, args=[])
    git.commit_hash = commit
    dep = BuildDependency(parent=None, config=config, workspace='packages', dep_source=git)
    dep.is_root = False  # tests rarely have a real parent chain
    dep.create_build_dir_if_needed()
    return dep


def make_git_and_mock_dep(name='libfoo', url='git@example.com:foo/libfoo.git', branch='main', tag='',
                          mamafile=None, **config_overrides):
    """Git dep_source + a Mock dep, no disk: for tests that drive Git's own clone/fetch methods.
    mamafile: the path the dep declares inside its own repository, which the version probe reads.
    Returns (git, dep)."""
    from mama.types.git import Git
    git = Git(name=name, url=url, branch=branch, tag=tag, mamafile=mamafile, shallow=True, args=[])
    dep = Mock(is_artifactory_shim=lambda: False, dep_source=git, target_args=[], from_artifactory=False,
               mamafile=None)   # dep.mamafile is the parent-repo override, and most deps declare none
    dep.name = name  # Mock(name=..) names the mock itself, not the attribute
    dep.src_dir = f'/packages/{name}'
    dep.config = Mock(print=False, verbose=False, update_stats=Mock(), deploy_stats=DeployStats(), **config_overrides)
    dep.config.target_matches.return_value = False   # a Mock reads truthy, so every dep would be the target
    return git, dep


def plain_config(sanitize=None, coverage=None):
    """A BuildConfig with only the fields the build-name rules read. BuildConfig.__init__ runs platform
    detection and CLI parsing, and a name test needs neither."""
    from mama.build_config import BuildConfig
    cfg = BuildConfig.__new__(BuildConfig)
    cfg.sanitize = sanitize
    cfg.coverage = coverage
    cfg.arch = None
    cfg.target_march = {}
    return cfg


def make_archive_name_target(*, sanitize=None, coverage=None, release=True, arch='x64', build_dir='',
                             version='abc1234', args=(), git_tag='', git_branch='', is_git=None,
                             version_suffix='', march=''):
    """Stub the BuildTarget surface artifactory_archive_name touches. compiler_version() and
    get_distro_info() read the host, so this answers with fixed values. `args` is what a consumer passed
    to add_git(..., args=[...]); the variant suffix comes from the same function a dep calls. Pass
    `git_tag` or `git_branch` for a REAL Git dep_source, the way add_git pins one. Pass `version=''` to
    let the name composer resolve the version field itself. `is_git=True` builds an unpinned git dep.
    `march` is what set_target_march pinned for `arch`."""
    from mama import build_names
    from mama.types.git import Git
    cfg = plain_config(sanitize, coverage)
    cfg.release = release
    cfg.arch = arch
    if march: cfg.target_march = {arch: march}
    cfg.compiler_version = lambda: 'gcc14'
    cfg.get_distro_info = lambda: ('linux', '24', 'noble')
    cfg.name = lambda: 'linux'
    if is_git or (is_git is None and (git_tag or git_branch)):
        dep_source = Git(name='pkg', url='https://example.com/pkg.git', branch=git_branch, tag=git_tag,
                         mamafile=None, shallow=True, args=[])
    else:
        dep_source = SimpleNamespace(is_pkg=False, fullname=None, is_git=False, is_src=False)
    dep_source.version_suffix = version_suffix  # what add_git(..., version_suffix=) put on the dep source
    dep = SimpleNamespace(is_root=False, dep_source=dep_source, target_args=list(args), build_dir=build_dir,
                          variant_suffix=build_names.build_variant_suffix(cfg, args))
    return SimpleNamespace(name='pkg', version=version, config=cfg, dep=dep)


def make_stub_target(tmp_path, platform_class=Linux, **config):
    """The smallest BuildTarget stand-in a helper module reads: a REAL platform, the two dirs and
    whatever config fields the caller names. Nothing here touches disk. Use platform_target instead
    when the test needs a real dep and a real build dir."""
    cfg = SimpleNamespace(**config)
    cfg.platform = platform_class(cfg)
    source, build = normalized_path(f'{tmp_path}/src'), normalized_path(f'{tmp_path}/build')
    return SimpleNamespace(config=cfg,
                           source_dir=lambda sub='': f'{source}/{sub}' if sub else source,
                           build_dir=lambda sub='': f'{build}/{sub}' if sub else build)


def make_includes_target(source_dir, build_dir=None):
    """Mock BuildTarget for the export_include and papa-deploy tests: the export lists, the includes
    root and the two dirs a deploy reads. A REAL Linux platform, so the deploy gets real extensions."""
    target = Mock()
    target.source_dir.return_value = normalized_path(source_dir)
    target.build_dir.return_value = normalized_path(build_dir or path_join(source_dir, 'build'))
    target.exported_includes, target.exported_libs = [], []
    target.exported_syslibs, target.exported_assets = [], []
    target.exported_modules = []
    target.strip_module_objects = True
    target.includes_root = ('', '', '')
    target.include_glob_filter = ['.h', '.hpp', '.hxx', '.hh']
    target.name = 'TestLib'
    target.config.platform = Linux(target.config)
    target.config.verbose = target.config.print = False
    target.config.target_march = {}       # a Mock dict answers .get() truthy and renames every build dir
    target.dep.from_artifactory = False   # a Mock reads truthy, and the deploy asks this
    target.dep.build_dir = target.build_dir()
    target.dep.variant_suffix = ''        # the papa `O` record appends it
    target.children.return_value = []     # a Mock is not iterable, and the module strip walks these
    return target


def make_includes_dep(target, name='TestLib', children=None):
    """Mock BuildDependency wrapping `target`, for the cmake-defines half of the same tests."""
    dep = Mock()
    dep.name = name
    dep.target = target
    dep.children = children or []
    dep.get_children.return_value = dep.children
    return dep


def archive_name_for(commit='abc1234', **kw) -> str:
    """The artifactory archive name one target shape resolves to, with the commit lookup stubbed so an
    unpinned git dep needs no clone. `kw` reaches make_archive_name_target."""
    from unittest.mock import patch
    from mama.artifactory import artifactory_archive_name
    from mama.types.git import Git
    with patch.object(Git, 'get_commit_hash', return_value=commit):
        return artifactory_archive_name(make_archive_name_target(**kw))


def linux_config(arch='x64', **overrides):
    """A REAL BuildConfig on linux, whatever the host runs. build_dir_name reads config.platform, so a
    Windows host would otherwise name the dir windows."""
    return platform_config(Linux, arch=arch, **overrides)


def touch_file(path) -> str:
    """Create an empty file and every parent dir it needs. Returns the path, so a test can assert on it."""
    os.makedirs(os.path.dirname(str(path)), exist_ok=True)
    open(str(path), 'w', encoding='utf-8').close()
    return str(path)


def deploy_pass_uploads(target) -> bool:
    """True when the deploy pass of this target reaches papa_upload_to. The deploy hook is stubbed, so
    only the upload decision answers."""
    from unittest.mock import patch
    with patch('mama.papa_upload.papa_upload_to') as upload, patch.object(type(target), 'deploy'):
        target._execute_deploy_tasks()
    return upload.called


_LATER_BUILD_REASONS = ('find_first_missing_build_product', 'find_missing_dependency',
                        'update_mamafile_tag', 'update_cmakelists_tag')


def should_build_reasons(dep, build_products=(), loaded_from_pkg=True, isolate=False):
    """Run _should_build for a non-target dep and return (built, the warnings it printed). The reason
    is what a user reads, so a test asserts on it and not only on the boolean.
    isolate: silence every reason that ranks below the source check, so only the source can decide"""
    from unittest.mock import Mock, patch
    dep.config.print = True
    target = Mock(build_products=list(build_products), args=[])
    target.name = dep.name
    dep.target = target  # the fall-through path reads it, so a mock config alone is not enough
    with contextlib.ExitStack() as stack:
        warned = stack.enter_context(patch('mama.build_dependency.warning'))
        if isolate:
            for name in _LATER_BUILD_REASONS: stack.enter_context(patch.object(dep, name, return_value=None))
        built = dep._should_build(dep.config, target, is_target=False, git_changed=False,
                                  loaded_from_pkg=loaded_from_pkg)
    return built, ' '.join(str(call) for call in warned.call_args_list)


def make_exporting_target(dep, includes, libs, version='abc1234', modules=None):
    """A BuildTarget that exports `includes` and `libs`, the way a mamafile package() leaves it."""
    from mama.build_target import BuildTarget
    target = BuildTarget(name=dep.name, config=dep.config, dep=dep, args=[])
    target.version = version
    target.exported_includes = includes
    target.exported_libs = libs
    target.exported_modules = modules or []
    return target


@lru_cache(maxsize=1)
def module_capable_compiler() -> dict:
    """The host compiler that builds C++20 modules: {name, cc, cxx, version}, or {} when there is none.
    Cmake scans the import graph only under Ninja, and only for a recent compiler. The paths are
    explicit, because compiler discovery composes a suffixed path a symlinked toolchain does not have."""
    if not shutil.which('ninja'): return {}
    cmake = execute_piped(['cmake', '--version'], throw=False) or ''
    version = re.search(r'(\d+)\.(\d+)', cmake)
    if not version or (int(version.group(1)), int(version.group(2))) < (3, 28): return {}
    # the same floors the generated cmake ships, so a capable host here is a capable host there
    for name, cc, cxx, least in (('clang', 'clang', 'clang++', 18), ('gcc', 'gcc', 'g++', 14)):
        cxx_path = shutil.which(cxx)
        if not cxx_path: continue
        dumped = (execute_piped([cxx_path, '-dumpversion'], throw=False) or '').strip()
        if dumped and int(dumped.split('.')[0]) >= least:
            return {'name': name, 'cc': shutil.which(cc), 'cxx': cxx_path, 'version': dumped}
    return {}


def archive_papa_package(package_path, archive_path) -> str:
    """Zip a deployed PAPA package the way papa_upload_to does, minus the FTP transfer, and run the same
    validation. Returns the archive path."""
    import zipfile
    from mama import papa_deploy, papa_upload
    papa = papa_deploy.PapaFileInfo(os.path.join(package_path, 'papa.txt'))
    archive = str(archive_path)
    with zipfile.ZipFile(archive, 'w') as zip:
        for _, entries in papa_upload._archive_groups(papa, package_path):
            for src, rel, _ in entries: zip.write(src, rel)
    papa_upload.validate_archive(package_path, papa, archive)
    return archive


def deploy_and_archive(tmp_path, target, package_path) -> str:
    """papa_deploy the target, then archive and validate what it deployed."""
    from unittest.mock import patch
    from mama import papa_deploy
    from mama.build_target import BuildTarget
    with patch.object(BuildTarget, 'children', lambda self: []):
        papa_deploy.papa_deploy_to(target, package_path, r_includes=False, r_dylibs=False,
                                   r_syslibs=False, r_assets=False)
    return archive_papa_package(package_path, tmp_path / 'package.zip')


def make_load_root(name='mylib', **config_overrides):
    """A root dep for load_dependency_chain: only the fields the loader itself touches. Serial by
    default, so a test drives one load at a time unless it asks for the parallel path."""
    root = Mock(already_loaded=False, should_rebuild=False, **{'get_children.return_value': []})
    root.name = name  # Mock(name=..) names the mock itself, not the attribute
    defaults = dict(serial_load=True, parallel_load=False, parallel_max=20, verbose=False, print=False)
    root.config = Mock(**(defaults | config_overrides))
    return root


def make_mock_local_dep(tmp_path, src_dir, name='libfoo', always_build=False, **config_overrides):
    """Real BuildDependency wired to a mock BuildConfig + a LocalSource pointing at an existing
    on-disk `src_dir`. build_dir is materialized so src_status round-trips."""
    from mama.build_dependency import BuildDependency
    from mama.types.local_source import LocalSource
    config = make_mock_config(tmp_path, **config_overrides)
    src = LocalSource(name=name, rel_path=str(src_dir), mamafile=None, always_build=always_build, args=[])
    dep = BuildDependency(parent=None, config=config, workspace='packages', dep_source=src)
    dep.is_root = False
    dep._update_dep_name_and_dirs(name)
    dep.create_build_dir_if_needed()
    return dep


_repo_templates = {}     # (branch, files) -> a built repo that a later call copies instead of rebuilding
_repo_template_dir = ''  # a session-lifetime dir, so a template outlives the test that built it


def set_repo_template_dir(path: str):
    """conftest names the dir that holds the git repo templates, and pytest removes it at the end."""
    global _repo_template_dir
    _repo_template_dir = path


def _build_git_repo(cwd, branch, files):
    os.makedirs(cwd, exist_ok=True)
    for name, text in (files or {}).items(): write_text_to(os.path.join(cwd, name), text)
    # --allow-empty: a repo with no file still needs the commit that every status read compares against
    for cmd in ['init -q' + (f' -b {branch}' if branch else ''), 'add -A', 'commit --allow-empty -q -m init']:
        execute_piped(['git', *cmd.split()], cwd=cwd)


def git_init_commit(cwd, branch='', files=None):
    """Turn `cwd` into a git repo and commit everything in it. The identity comes from the environment,
    which conftest names once.
    branch: name the initial branch, for a test that pins one
    files: {name: text} to write before the commit

    The first call for one (branch, files) pair builds the repo with git and keeps a copy. Every later
    call copies that one, which costs 24 ms on Windows against 158 ms for three git spawns."""
    cwd = str(cwd)
    # a caller that wrote its own files first gets a real build, because the key describes `files` alone
    if os.path.isdir(cwd) and os.listdir(cwd): return _build_git_repo(cwd, branch, files)
    key = (branch, tuple(sorted((files or {}).items())))
    if key in _repo_templates:
        shutil.copytree(_repo_templates[key], cwd, dirs_exist_ok=True)
        return
    _build_git_repo(cwd, branch, files)
    if _repo_template_dir:
        _repo_templates[key] = template = path_join(_repo_template_dir, str(len(_repo_templates)))
        shutil.copytree(cwd, template)


def git_run(args, cwd):
    """One git command against `cwd`, captured. For a test that drives git itself, so it never asserts
    against a copy of git rules. `SubProcess.run` would give the child a TTY nobody here reads."""
    return subprocess.run(['git', *args], cwd=str(cwd), capture_output=True, text=True)


def source_tree_changed(dep) -> bool:
    """Ask whether a build input moved since the last build, with the per-run memo dropped first.
    A test edits the tree itself, so the memo would answer with the state before the edit."""
    from mama.utils.git_status import forget_git_dir_fingerprint
    forget_git_dir_fingerprint(dep.src_dir)
    return dep.dep_source.source_tree_changed(dep)


def make_git_root_with_local_pkgs(tmp_path, count=1):
    """A git repo at `tmp_path/root` holding `count` committed local packages under `libs/`. Returns the
    list of BuildDependency, one per package, which share the one enclosing repo."""
    root = tmp_path / 'root'
    git_init_commit(root, files={f'libs/pkg{i}/lib.cpp': f'int f{i}(){{ return {i}; }}\n' for i in range(count)})
    return [make_mock_local_dep(tmp_path, src_dir=root / 'libs' / f'pkg{i}', name=f'pkg{i}')
            for i in range(count)]


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
    defaults = {'jobs': 8, 'coverage': False, 'clang_tidy': False}  # a test may override any of them
    dep = make_mock_local_dep(tmp_path, src_dir=sub, **{**defaults, **config_overrides})
    dep.config.get_preferred_compiler_paths.return_value = compiler
    return dep.target, dep


def run_config_capturing(target, dep, out=None, raises=None, leave_build_dir=False):
    """Drive cmake configure.run_config with the cmake call + seed coordinator stubbed. Returns the
    configure command lines it would have run, so a test can assert on the flags without a real cmake.
    out: the target output sink, which the scheduler passes and mamabuild.log records
    raises: what the stubbed cmake call raises, for the failed-configure paths
    leave_build_dir: write the cache and the build file a real cmake leaves, so the next call can skip"""
    from unittest.mock import patch
    from mama.buildsys.cmake import configure as cmake_configure
    cmds = []
    def conf(cmd, *a, **k):
        cmds.append(cmd)
        if raises: raise raises
    with patch('mama.buildsys.cmake.configure._rerunnable_cmake_conf', side_effect=conf), \
         patch('mama.buildsys.cmake.configure.compute_env', return_value={}), \
         patch('mama.buildsys.cmake.configure._seed_coordinator') as coord, \
         patch.object(dep, 'get_enabled_sanitizers', return_value=''):
        coord.return_value.prepare.return_value = 'none'
        coord.return_value.status.return_value = ('fp', False)
        cmake_configure.run_config(target, out=out)
        if leave_build_dir:
            write_cmake_cache(target.build_dir(), 'CMAKE_GENERATOR:INTERNAL=Ninja\n')
            write_build_file(target.build_dir())
    return cmds


def configure_cmd(tmp_path, generator, platform_class=None, cmake_opts=(), **config_overrides) -> str:
    """The cmake configure command line mama builds for one target. `generator` replaces the detected
    one, which a mock config cannot resolve for MSVC or Xcode. `cmake_opts` is what a mamafile added."""
    from unittest.mock import patch
    target, dep = make_configured_target(tmp_path, **config_overrides)
    if platform_class: set_mock_platform(dep.config, platform_class)
    if cmake_opts: target.add_cmake_options(list(cmake_opts))
    with patch('mama.buildsys.cmake.configure._generator', return_value=generator):
        return run_config_capturing(target, dep)[0]


def write_dep_exports(target, text):
    """Write mama-dependencies.cmake, which names the include dirs and libs the dependencies export.
    The configure fingerprint hashes it, so an edit here is what makes a parent reconfigure."""
    write_text_to(path_join(target.build_dir(), 'mama-dependencies.cmake'), text)


def make_cmake_detection(build_files_dir, langs=('C', 'CXX', 'RC'), vs=True, partial=(), system='Windows'):
    """A `CMakeFiles/<ver>` dir as cmake leaves it after detection, for the compiler-seed tests.

    Each language names a compiler that EXISTS in the dir, because the seed cache refuses a module
    whose compiler it cannot stat. A language in `partial` stops at stage 1 (no ABI probe), the way
    a killed configure leaves it. Returns the dir."""
    from mama.buildsys.cmake.compiler_cache import _LANG_FILES
    os.makedirs(build_files_dir, exist_ok=True)
    write = lambda name, text: open(os.path.join(build_files_dir, name), 'w').write(text)
    write('CMakeSystem.cmake', f'set(CMAKE_SYSTEM_NAME "{system}")\n')
    for lang in langs:
        mod, abi = _LANG_FILES[lang]
        compiler = normalized_path(os.path.join(build_files_dir, f'cc_{lang}'))
        open(compiler, 'w').write('')
        complete = abi and lang not in partial
        write(mod, f'set(CMAKE_{lang}_COMPILER "{compiler}")\n' + \
                   (f'set(CMAKE_{lang}_ABI_COMPILED TRUE)\n' if complete else ''))
        if complete: open(os.path.join(build_files_dir, abi), 'wb').write(b'\x00abi')
    if vs: write('VCTargetsPath.txt', 'C:/VCTargets\n')
    return build_files_dir


def write_files(root, files:dict):
    """Write {relative path: text} under `root`, creating each parent dir."""
    for rel, text in files.items():
        os.makedirs(os.path.dirname(f'{root}/{rel}'), exist_ok=True)
        open(f'{root}/{rel}', 'w').write(text)


def write_cmake_cache(build_dir, text):
    """Write a raw CMakeCache.txt into build_dir (created if missing)."""
    os.makedirs(build_dir, exist_ok=True)
    with open(os.path.join(build_dir, 'CMakeCache.txt'), 'w', encoding='utf-8') as f: f.write(text)


def write_build_file(build_dir, name='build.ninja'):
    """Write the generator's build file so is_cmake_cache_valid() sees a completed configure."""
    with open(os.path.join(build_dir, name), 'w', encoding='utf-8') as f: f.write('# generated\n')


# What mama generates into a test project. The copy starts without them, so no test has to remove them.
_GENERATED = shutil.ignore_patterns('packages', 'bin', 'build', '__pycache__', '*.pyc')


def init(caller_file: str, workdir) -> str:
    """Copy the test project next to `caller_file` into `workdir`, then chdir into the copy. Pass the
    tmp_path fixture as `workdir`. Returns the new working directory. Two pytest sessions can then run
    the same test at the same time. In a shared project dir, the `mama update` of one run clones into
    the tree the other run builds."""
    project = os.path.join(str(workdir), 'project')
    shutil.copytree(os.path.dirname(os.path.abspath(caller_file)), project, ignore=_GENERATED)
    os.chdir(project)
    return project

# The example remote a git integration test clones. Two commits: `old` lacks the REMOTE_VERSION line and
# `new` has it, so a test reads remote.h to prove which one the pin resolved to.
_REMOTE_HEADER = '#pragma once\n#include <string>\n{version}namespace example {{ void print_remote(const std::string& s); }}\n'
_REMOTE_FILES = {
    'CMakeLists.txt': 'cmake_minimum_required(VERSION 3.6)\nproject(example_remote)\n'
                      'add_library(ExampleRemote STATIC remote.cpp remote.h)\n'
                      'install(TARGETS ExampleRemote DESTINATION bin)\n',
    'remote.cpp': '#include "remote.h"\n#include <cstdio>\n'
                  'namespace example { void print_remote(const std::string& s) { printf("%s\\n", s.c_str()); } }\n',
    'README.md': 'Example remote for the mama git tests.\n',
}
# A pin test reads remote.h to learn which commit it got, and never opens a build artifact. This mamafile
# turns the cmake configure and build off, which costs about 2.5 seconds per clone on Windows.
_REMOTE_MAMAFILE = 'import mama\n\nclass ExampleRemote(mama.BuildTarget):\n    def build(self):\n' \
                   '        self.nothing_to_build()\n'


@pytest.fixture(autouse=True)
def unmemoized_git_fingerprints():
    """Ask git every time, for a test that edits a working tree itself. mama memoizes the fingerprint per
    source dir, because during a build only mama writes a dependency tree and run_git drops the memo when
    it does. A test writes files directly, so the memo would answer with the state before the edit.
    Import this into the conftest of a directory whose tests edit a tree."""
    from mama.utils import git_status as util
    util.memoize_git_fingerprints = False
    util._git_fingerprints.clear()
    yield
    util.memoize_git_fingerprints = True


def make_example_remote(work_dir, buildable=False) -> dict:
    """Build the example remote as a local bare repo, so no git test reaches the network. It carries the
    shape every pin test needs. Commit `old` lacks the REMOTE_VERSION line and commit `new` has it.
    Each one gets a tag, v1.0.0 and v2.0.0, and a branch, `old` and `master`.
    buildable: ship no mamafile, so mama configures and builds the clone. Only a test that links the
               library needs that. Every other test reads a source file and pays 2.5 seconds for nothing.
    Returns {url, old, new}, where url is a file:// url and both values are full commit hashes."""
    work = os.path.join(str(work_dir), 'work')
    os.makedirs(work, exist_ok=True)
    def git(*args) -> str:
        return execute_piped(['git', *args], cwd=work) or ''
    git('init', '-q', '-b', 'master')
    files = _REMOTE_FILES if buildable else {**_REMOTE_FILES, 'mamafile.py': _REMOTE_MAMAFILE}
    for name, text in files.items(): write_text_to(os.path.join(work, name), text)
    for version, tag in (('', 'v1.0.0'), ('#define REMOTE_VERSION 2\n', 'v2.0.0')):
        write_text_to(os.path.join(work, 'remote.h'), _REMOTE_HEADER.format(version=version))
        git('add', '-A'); git('commit', '-q', '-m', tag); git('tag', tag)
    git('branch', 'old', 'v1.0.0')  # the branch pins need a second branch behind master
    bare = os.path.join(str(work_dir), 'MamaExampleRemote.git')
    execute_piped(['git', 'clone', '--bare', '-q', work, bare])
    # file:// and not a plain path: git ignores --depth on a plain local clone, and mama clones shallow
    return {'url': 'file:///' + normalized_path(bare).lstrip('/'),
            'old': git('rev-parse', 'v1.0.0^{commit}'), 'new': git('rev-parse', 'v2.0.0^{commit}')}


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

@lru_cache(maxsize=1)  # the filesystem cannot change mid-run, so probe it once
def has_case_sensitive_fs() -> bool:
    """True when the pytest temp filesystem keeps two spellings of one name. Windows and macOS hold ONE
    dir for `QCoro/` and `qcoro/`, so a test that deploys both cannot run there."""
    with tempfile.TemporaryDirectory(dir=os.environ.get('PYTEST_DEBUG_TEMPROOT')) as tmp:
        with open(os.path.join(tmp, 'CaseProbe'), 'w'): pass
        return not os.path.exists(os.path.join(tmp, 'caseprobe'))

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

