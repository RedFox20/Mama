"""Pins compiler-flip ordering: root locks + re-resolves, so no run scatters deps across linux/ and linux-clang/."""
from mama.build_config import BuildConfig
from testutils import make_mock_dep


def _linux_cfg():
    c = BuildConfig([])
    c.msvc = c.macos = c.ios = c.android = c.raspi = False
    c.mips = c.oclea = c.xilinx = c.imx8mp = c.yocto_linux = None
    c.linux = True; c.arch = 'x64'; c.print = False
    return c


def test_a_late_prefer_clang_cannot_flip_a_locked_compiler():
    c = _linux_cfg()
    c.lock_compiler()
    c.prefer_clang('some_dep')  # dep settings() runs after the root decided
    assert not c.clang


def test_the_root_may_still_pick_the_compiler_before_the_lock():
    c = _linux_cfg()
    c.compiler_cmd = False  # no explicit clang/gcc on the cmdline
    c.prefer_clang('root')
    c.lock_compiler()
    assert c.clang and c.platform_build_dir_name() == 'linux-clang'


def test_dirs_re_resolve_after_the_compiler_flips(tmp_path):
    # build_dir is computed at BuildTarget construction, before settings() reaches prefer_clang
    dep = make_mock_dep(tmp_path)
    dep.config.platform_build_dir_name.return_value = 'linux'
    dep._update_dep_name_and_dirs(dep.name)
    assert dep.build_dir.endswith('/linux')
    dep.config.platform_build_dir_name.return_value = 'linux-clang'
    dep._update_dep_name_and_dirs(dep.name)
    assert dep.build_dir.endswith('/linux-clang')
