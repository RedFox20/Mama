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
    assert c.build_dir_suffix() == ''
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
