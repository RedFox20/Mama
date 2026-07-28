"""Pins the CLI arg to platform mapping and the class contract every platform must satisfy."""
import inspect
import pytest

from mama.build_config import BuildConfig
from mama.platforms.platform import Platform
from mama.platforms.registry import PLATFORMS, platform_for_arg, host_platform, platform_named


# --- CLI args ---

@pytest.mark.parametrize('arg,name,arch', [
    ('windows', 'windows', None), ('msvc', 'windows', None),
    ('linux', 'linux', None), ('macos', 'macos', None), ('ios', 'ios', None),
    ('android', 'android', None), ('raspi', 'raspi', None), ('raspi32', 'raspi', 'arm'),
    ('oclea', 'oclea', None), ('xilinx', 'xilinx', None), ('imx8mp', 'imx8mp', None),
    ('mips', 'mips', None),
])
def test_a_cli_arg_selects_its_platform_and_arch(arg, name, arch):
    cls, pinned = platform_for_arg(arg)
    assert cls.name == name and pinned == arch


@pytest.mark.parametrize('arg', ['riscv', 'build', 'gcc', ''])
def test_an_unknown_arg_names_no_platform(arg):
    assert platform_for_arg(arg) is None


@pytest.mark.parametrize('arg,name,arch,build_dir', [
    ('linux', 'linux', 'x64', 'linux'), ('windows', 'windows', 'x64', 'windows'),
    ('macos', 'macos', 'arm64', 'macosarm'), ('ios', 'ios', 'arm64', 'ios'),
    ('android', 'android', 'arm64', 'android'), ('raspi', 'raspi', 'arm64', 'raspi'),
    ('raspi32', 'raspi', 'arm', 'raspi32'), ('mips', 'mips', 'mipsel', 'mips'),
    ('oclea', 'oclea', 'arm64', 'oclea'), ('xilinx', 'xilinx', 'arm64', 'xilinx'),
    ('imx8mp', 'imx8mp', 'arm64', 'imx8mp'),
])
def test_the_arg_drives_the_whole_config(arg, name, arch, build_dir):
    config = BuildConfig([arg])
    assert config.name() == name
    assert config.arch == arch
    assert config.platform_build_dir_name() == build_dir


def test_the_host_platform_is_used_when_no_arg_names_one():
    assert BuildConfig([]).platform is not None
    assert host_platform() in PLATFORMS


def test_platform_named_rejects_an_unknown_name():
    with pytest.raises(KeyError, match='No platform named'):
        platform_named('riscv')


# --- the class contract ---

@pytest.mark.parametrize('platform_class', PLATFORMS, ids=lambda p: p.name)
def test_every_platform_declares_a_complete_identity(platform_class):
    assert issubclass(platform_class, Platform)
    assert platform_class.name, 'a platform without a name cannot be selected or named in an archive'
    assert platform_class.supported_arches
    default = platform_class.default_arch
    assert not default or default in platform_class.supported_arches
    assert set(platform_class.build_dirs) <= set(platform_class.supported_arches)


def test_no_two_platforms_share_a_name():
    names = [p.name for p in PLATFORMS]
    assert len(set(names)) == len(names)


def test_no_two_platform_and_arch_pairs_share_a_build_dir():
    """A shared build dir means one platform's cache and libs clobber the other's."""
    dirs = {}
    for platform_class in PLATFORMS:
        for arch in platform_class.supported_arches:
            config = BuildConfig([])
            config.set_platform_class(platform_class)
            config.arch = arch
            name = config.platform_build_dir_name()
            assert name not in dirs, f'{platform_class.name}/{arch} shares {name} with {dirs.get(name)}'
            dirs[name] = f'{platform_class.name}/{arch}'


_HOOKS = ('get_cmake_build_opts', 'get_cxx_flags', 'get_ld_flags', 'init_toolchain', 'init_default',
          'build_dir_name', 'distro_version', 'compiler_version_tag', 'lib_extensions', 'inject_env')


@pytest.mark.parametrize('platform_class', PLATFORMS, ids=lambda p: p.name)
@pytest.mark.parametrize('hook', _HOOKS)
def test_every_platform_keeps_the_base_parameters(platform_class, hook):
    """One vocabulary. A platform that renames or re-orders a hook parameter is never called correctly
    through the base. Extra optional parameters are fine, so Mips can still take an arch."""
    def params(cls): return list(inspect.signature(getattr(cls, hook)).parameters)
    base = params(Platform)
    assert params(platform_class)[:len(base)] == base, f'{platform_class.name}.{hook}'


@pytest.mark.parametrize('platform_class', PLATFORMS, ids=lambda p: p.name)
def test_every_cross_platform_says_so(platform_class):
    """is_cross drives the seed cache and the host-tool bootstrap. A cross platform that reports
    itself as native silently reuses the host's compiler detection."""
    cross = platform_class.name not in ('windows', 'linux', 'macos')
    assert platform_class.is_cross == cross
    assert platform_class.is_host_runnable == (not cross)
