import pytest
from testutils import platform_config
from mama.platforms.linux import Linux
from mama.platforms.macos import Macos
from mama.platforms.ios import Ios
from mama.platforms.android import Android
from mama.platforms.windows import Windows
from mama.platforms.oclea import Oclea


def linux_config():
    """A BuildConfig pinned to linux/x64 so dir names are host-independent."""
    return platform_config(Linux, 'x64')


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


@pytest.mark.parametrize('platform_class', [Macos, Ios, Android, Windows, Oclea])
def test_non_linux_platforms_are_unaffected_by_clang(platform_class):
    # set_platform() is exclusive: these never see the suffix, toolset/SDK fixes their compiler
    c = platform_config(platform_class, clang=True)
    assert '-clang' not in c.platform_build_dir_name()


def test_a_yocto_board_is_named_by_its_own_build_dir():
    assert platform_config(Oclea).platform_build_dir_name() == 'oclea'
