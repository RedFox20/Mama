import os
import re
import shutil
import subprocess
import sys
import threading
from typing import Iterable, Optional
from unittest.mock import Mock

import mama
import pytest

_ANSI = re.compile(r'\x1b\[[0-9;]*[A-Za-z]')  # SGR colours + cursor moves
def strip_ansi(s: str) -> str: return _ANSI.sub('', s)


class FakeBuildTarget:
    """Base for the runner-test target fakes: the build-weight stubs the parallel runners call on
    every dep (configure/build phase bodies and event recording stay specialised per test)."""
    _build_jobs = None
    def _has_custom_build(self): return False
    def _reserved_cores(self): return 4


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
    cfg.run_cmake_configure = False
    cfg.target = None
    cfg.clean_only.return_value = False  # Mock methods are truthy by default
    cfg.list = False
    # platform aliases (BuildTarget.__init__ pokes these)
    cfg.msvc = False
    cfg.linux = True
    cfg.macos = False
    cfg.ios = False
    cfg.android = None
    cfg.raspi = False
    cfg.oclea = None
    cfg.xilinx = None
    cfg.mips = None
    cfg.imx8mp = None
    cfg.yocto_linux = None
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
    return cfg


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