from types import SimpleNamespace
from mama.build_config import BuildConfig


def linux_config():
    """A BuildConfig pinned to linux/x64 so dir names are host-independent."""
    c = BuildConfig([])
    c.msvc = c.macos = c.ios = c.android = c.raspi = False
    c.mips = c.oclea = c.xilinx = c.imx8mp = c.yocto_linux = None
    c.linux = True
    c.arch = 'x64'
    return c


def test_no_sanitizer_dir_unchanged():
    c = linux_config()
    assert c.build_dir_with_suffix('linux') == 'linux'
    assert c.platform_build_dir_name() == 'linux'


def test_each_sanitizer_gets_own_dir():
    c = linux_config()
    for sanitize, expected in [('address', 'linux-asan'),
                               ('thread',  'linux-tsan'),
                               ('undefined', 'linux-ubsan'),
                               ('leak',    'linux-lsan')]:
        c.sanitize = sanitize
        assert c.platform_build_dir_name() == expected


def test_combined_sanitizers_stay_distinct():
    c = linux_config()
    c.sanitize = 'address,undefined'
    assert c.platform_build_dir_name() == 'linux-asan-ubsan'


def test_coverage_gets_own_dir():
    c = linux_config()
    c.coverage = 'default'
    assert c.platform_build_dir_name() == 'linux-coverage'


def test_coverage_composes_with_sanitizer():
    c = linux_config()
    c.coverage = 'default'
    c.sanitize = 'address'
    assert c.platform_build_dir_name() == 'linux-coverage-asan'


def test_clang_gets_its_own_dir_and_gcc_keeps_the_bare_name():
    c = linux_config()
    assert c.platform_build_dir_name() == 'linux'  # gcc default: no churn for existing trees
    c.clang = True; c.gcc = False
    assert c.platform_build_dir_name() == 'linux-clang'


def test_compiler_is_the_coarsest_suffix():
    c = linux_config()
    c.clang = True; c.sanitize = 'thread'
    assert c.platform_build_dir_name() == 'linux-clang-tsan'
    c.coverage = 'default'; c.sanitize = 'address'
    assert c.platform_build_dir_name() == 'linux-clang-coverage-asan'


def test_arm_linux_also_gets_the_clang_suffix():
    c = linux_config()
    c.arch = 'arm64'; c.clang = True
    assert c.platform_build_dir_name() == 'linuxarm-clang'


def test_non_linux_platforms_are_unaffected_by_clang():
    # set_platform() is exclusive: these never see the suffix, toolset/SDK fixes their compiler
    for platform in ('macos', 'ios', 'android', 'msvc'):
        c = linux_config()
        c.linux = False; setattr(c, platform, True); c.clang = True
        assert '-clang' not in c.platform_build_dir_name()
    yocto = linux_config()
    yocto.linux = False; yocto.yocto_linux = SimpleNamespace(build_dir='oclea'); yocto.clang = True
    assert yocto.platform_build_dir_name() == 'oclea'
