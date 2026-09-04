"""Pins the generic aarch64 linux platform: a plain GNU cross toolchain, no vendor SDK anywhere."""
from unittest.mock import Mock

import pytest

from testutils import executable_extension, make_configured_target, set_mock_platform
from mama.build_config import BuildConfig
from mama.platforms.aarch64 import Aarch64
from mama.platforms.raspi import Raspi
from mama.buildsys.cmake import configure as cc
from mama.build_names import build_dir_name


def _aarch64(**over):
    config = Mock(arch='arm64', print=False, target_march={}, **over)
    config.append_env_path = lambda paths, env: None
    return Aarch64(config)


# --- arg selection ---

def test_the_platform_arg_selects_it():
    config = BuildConfig(['aarch64'])
    assert config.name() == 'aarch64'
    assert config.arch == 'arm64' and config.aarch64


def test_aarch64_after_another_platform_is_an_error_and_not_a_silent_switch():
    """`aarch64` used to be an alias of the arm64 arch. Someone still spelling it that way must be
    told, not handed an android build that quietly cross-compiled for linux instead."""
    with pytest.raises(RuntimeError, match='pin the arch with `arm64`'):
        BuildConfig(['android', 'aarch64'])


def test_arm64_still_pins_the_arch_of_another_platform():
    android = BuildConfig(['android', 'arm64'])
    assert android.name() == 'android' and android.arch == 'arm64'


def test_the_cmake_define_is_not_a_bare_aarch64():
    """mama.cmake matches the arm64 arch with a regex whose own text is `(aarch64)|(AARCH64)|...`,
    and CPU-detection code everywhere sets a plain AARCH64. A guard by that name would fire for a
    consumer that set it for its own reasons, and send the build to the wrong package dir."""
    assert Aarch64.platform_define == 'AARCH64_LINUX'


def test_the_name_holds_no_hyphen():
    """The artifactory archive name joins its fields with `-` and counts them to find the version, so
    a two-token platform field would shift the version out of place and `unpublish` would delete the
    wrong archives."""
    assert '-' not in Aarch64.name


def test_a_mamafile_reads_the_platform_off_self(tmp_path):
    """README documents `self.aarch64`. A missing forwarded property is an AttributeError on
    every platform, not just this one."""
    t, dep = make_configured_target(tmp_path, arch='arm64')
    assert t.aarch64 is None
    set_mock_platform(dep.config, Aarch64)
    assert t.aarch64 is dep.config.platform


def test_a_mamafile_selects_flags_by_the_platform_name(tmp_path):
    """`self.select(aarch64=...)` is what add_platform_cxx_flags and add_platform_options run on.
    The name has to stay a valid python keyword for that to reach anything."""
    t, dep = make_configured_target(tmp_path, arch='arm64')
    set_mock_platform(dep.config, Aarch64)
    assert t.select(linux='-fPIC', aarch64='-mcpu=cortex-a53') == '-mcpu=cortex-a53'
    t.add_platform_cxx_flags(aarch64='-mcpu=cortex-a53')
    assert '-mcpu' in t.cmake_cxxflags


@pytest.mark.parametrize('arch', ['x64', 'x86', 'arm', 'mips'])
def test_it_rejects_every_arch_but_arm64(arch):
    with pytest.raises(RuntimeError, match='Unsupported arch'):
        BuildConfig(['aarch64', f'arch={arch}'])


def test_it_does_not_share_a_build_dir_with_raspi():
    """Both cross-compile with aarch64-linux-gnu. One build dir would let one platform's cache and
    libs clobber the other's."""
    assert build_dir_name(BuildConfig(['aarch64'])) == 'aarch64'
    assert build_dir_name(BuildConfig(['raspi'])) == 'raspi'


# --- the toolchain that reaches cmake ---

def test_it_cross_compiles_for_aarch64_and_never_the_host(tmp_path):
    t, dep = make_configured_target(tmp_path, arch='arm64')
    set_mock_platform(dep.config, Aarch64).init_toolchain('/opt/aarch64')
    opts = cc._platform_opts(t)
    assert 'AARCH64_LINUX=TRUE' in opts
    assert 'CMAKE_SYSTEM_NAME=Linux' in opts
    assert 'CMAKE_SYSTEM_PROCESSOR=aarch64' in opts
    # NEVER, so cmake takes the cross binutils beside the compiler and not the host's
    assert 'CMAKE_FIND_ROOT_PATH_MODE_PROGRAM=NEVER' in opts


def test_the_triple_drives_the_compiler_paths(tmp_path):
    (tmp_path / 'bin').mkdir()
    p = _aarch64()
    p.init_toolchain(str(tmp_path))
    assert p.triple() == 'aarch64-linux-gnu'
    assert p.compiler_prefix() == f'{tmp_path}/bin/aarch64-linux-gnu-'
    assert p.archiver() == f'{tmp_path}/bin/aarch64-linux-gnu-ar'
    tc = p.toolchain()
    assert tc.cc == f'{tmp_path}/bin/aarch64-linux-gnu-gcc'
    assert tc.cxx == f'{tmp_path}/bin/aarch64-linux-gnu-g++'


def test_a_distro_cross_package_passes_no_sysroot(tmp_path):
    """Debian's g++-aarch64-linux-gnu has no <triple>/sysroot. Passing one that is not there makes
    every compile fail on missing system headers."""
    (tmp_path / 'bin').mkdir()
    p = _aarch64()
    p.init_toolchain(str(tmp_path))
    assert p.get_sysroot() == '' and p.get_includes() == []
    flags = {}
    p.get_cxx_flags(lambda f, v='': flags.__setitem__(f, v))
    assert '--sysroot' not in flags and flags['-march'] == 'armv8-a' and '-mfpu' not in flags


def test_a_standalone_toolchain_still_passes_its_sysroot(tmp_path):
    (tmp_path / 'bin').mkdir()
    (tmp_path / 'aarch64-linux-gnu' / 'sysroot').mkdir(parents=True)
    p = _aarch64()
    p.init_toolchain(str(tmp_path))
    flags = {}
    p.get_cxx_flags(lambda f, v='': flags.__setitem__(f, v))
    assert flags['--sysroot'] == f'{tmp_path}/aarch64-linux-gnu/sysroot'


# --- discovery ---

def test_the_toolchain_is_found_in_a_plain_bin_layout(tmp_path):
    """apt install g++-aarch64-linux-gnu puts the compiler straight in /usr/bin/."""
    (tmp_path / 'bin').mkdir(parents=True)
    (tmp_path / 'bin' / f'aarch64-linux-gnu-gcc{executable_extension()}').write_text('')
    p = _aarch64()
    p._search_paths = lambda: [str(tmp_path)]
    p.init_default()
    assert p.compiler_prefix() == f'{tmp_path}/bin/aarch64-linux-gnu-'


def test_a_missing_toolchain_raises_instead_of_falling_back_to_the_host(tmp_path):
    """A silent fallback would build with the HOST gcc and quietly produce x86 binaries."""
    p = _aarch64()
    p._search_paths = lambda: [str(tmp_path)]
    with pytest.raises(EnvironmentError, match='aarch64-linux-gnu-gcc'):
        p.init_default()


def test_the_env_override_is_searched_before_the_defaults(monkeypatch):
    """A user pointing AARCH64_LINUX_HOME at their own toolchain must beat whatever is in /usr."""
    monkeypatch.setenv('AARCH64_HOME', '/opt/my-toolchain')
    config = BuildConfig(['aarch64'])
    assert config.platform._search_paths()[0] == '/opt/my-toolchain'


# --- shared base, and what this platform does NOT inherit from raspi ---

def test_it_does_not_search_the_broadcom_layout(tmp_path):
    """Raspi nests the legacy 32-bit toolchain under arm-bcm2708/<triple>/. Nothing generic does, and
    a search that walks it would resolve a Pi toolchain for a board that is not a Pi."""
    nested = tmp_path / 'arm-bcm2708' / 'aarch64-linux-gnu' / 'bin'
    nested.mkdir(parents=True)
    (nested / f'aarch64-linux-gnu-gcc{executable_extension()}').write_text('')
    p = _aarch64()
    p._search_paths = lambda: [str(tmp_path)]
    with pytest.raises(EnvironmentError, match='aarch64-linux-gnu-gcc'):
        p.init_default()


def test_the_supported_arches_come_from_the_declared_triples():
    assert Aarch64.supported_arches == ('arm64',)
    assert Raspi.supported_arches == ('arm64', 'arm')


def test_a_board_derived_from_another_keeps_its_display_name():
    """The name a message uses is READ, never assigned onto the subclass. Assigning it made a
    subclass of Raspi report its own bare name and lose 'Raspberry PI'."""
    class RaspiZero(Raspi):
        name = 'raspizero'
    assert RaspiZero(Mock(arch='arm64', print=False, target_march={}))._name() == 'Raspberry PI'
    assert Aarch64(Mock(arch='arm64', print=False, target_march={}))._name() == 'AArch64 Linux'
