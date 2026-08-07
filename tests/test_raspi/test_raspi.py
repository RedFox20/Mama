"""Pins raspi as a 64-bit ARMv8 platform with legacy 32-bit ARMv7 under `raspi32`."""
import os
from unittest.mock import Mock

import pytest

from testutils import executable_extension, make_configured_target, set_mock_platform
from mama.build_config import BuildConfig
from mama.platforms.raspi import Raspi
from mama.buildsys.cmake import configure as cc
from mama.build_names import build_dir_name


def _raspi(arch='arm64', **over):
    config = Mock(arch=arch, print=False, target_march={}, **over)
    config.append_env_path = lambda paths, env: None
    return Raspi(config)


# --- arch selection ---

def test_raspi_defaults_to_arm64():
    """Every Pi since the 3 is ARMv8. 32-bit is the legacy path, not the default."""
    config = BuildConfig(['raspi'])
    assert config.arch == 'arm64' and config.raspi


def test_raspi32_selects_the_legacy_armv7_build():
    config = BuildConfig(['raspi32'])
    assert config.arch == 'arm' and config.raspi


def test_raspi_accepts_an_explicit_arch():
    assert BuildConfig(['raspi', 'arm']).arch == 'arm'
    assert BuildConfig(['raspi', 'arm64']).arch == 'arm64'


@pytest.mark.parametrize('arch', ['x64', 'x86', 'mips'])
def test_raspi_rejects_an_unsupported_arch(arch):
    with pytest.raises(RuntimeError, match='Unsupported arch'):
        BuildConfig(['raspi', f'arch={arch}'])


# --- the two arches never share a build dir ---

def test_the_build_dir_follows_the_arch():
    assert build_dir_name(BuildConfig(['raspi'])) == 'raspi'
    assert build_dir_name(BuildConfig(['raspi32'])) == 'raspi32'


# --- everything derives from the triple ---

@pytest.mark.parametrize('arch,triple,processor,march', [
    ('arm64', 'aarch64-linux-gnu', 'aarch64', 'armv8-a'),
    ('arm', 'arm-linux-gnueabihf', 'armv7-a', 'armv7-a'),
])
def test_the_triple_drives_compiler_sysroot_and_includes(arch, triple, processor, march, tmp_path):
    (tmp_path / 'bin').mkdir()
    (tmp_path / triple / 'sysroot').mkdir(parents=True)      # standalone toolchain layout
    (tmp_path / triple / 'lib' / 'include').mkdir(parents=True)
    raspi = _raspi(arch)
    raspi.init_toolchain(str(tmp_path))
    assert raspi.triple() == triple
    assert raspi.system_processor() == processor
    assert raspi.compiler_prefix() == f'{tmp_path}/bin/{triple}-'
    assert raspi.get_sysroot() == f'{tmp_path}/{triple}/sysroot'
    assert raspi.get_includes() == [f'{tmp_path}/{triple}/lib/include']
    flags = {}
    raspi.get_cxx_flags(lambda f, v='': flags.__setitem__(f, v))
    assert flags['-march'] == march and flags['--sysroot'] == f'{tmp_path}/{triple}/sysroot'


def test_the_legacy_armv7_build_declares_an_fpu(tmp_path):
    """armv7-a alone declares no FPU, and the gnueabihf triple defaults to -mfloat-abi=hard, so gcc
    refuses with "selected architecture lacks an FPU" and nothing compiles at all."""
    (tmp_path / 'bin').mkdir()
    raspi = _raspi('arm')
    raspi.init_toolchain(str(tmp_path))
    flags = {}
    raspi.get_cxx_flags(lambda f, v='': flags.__setitem__(f, v))
    assert flags['-march'] == 'armv7-a' and flags['-mfpu'] == 'neon-vfpv4'


def test_the_arm64_build_needs_no_fpu_flag(tmp_path):
    (tmp_path / 'bin').mkdir()
    raspi = _raspi('arm64')
    raspi.init_toolchain(str(tmp_path))
    flags = {}
    raspi.get_cxx_flags(lambda f, v='': flags.__setitem__(f, v))
    assert flags['-march'] == 'armv8-a' and '-mfpu' not in flags


@pytest.mark.parametrize('arch,processor', [('arm64', 'aarch64'), ('arm', 'armv7-a')])
def test_the_cmake_system_processor_is_the_target_not_the_host(arch, processor, tmp_path):
    t, dep = make_configured_target(tmp_path, arch=arch)
    set_mock_platform(dep.config, Raspi).init_toolchain('/opt/rpi')
    opts = cc._platform_opts(t)
    assert 'RASPI=TRUE' in opts
    assert f'CMAKE_SYSTEM_PROCESSOR={processor}' in opts
    assert 'CMAKE_SYSTEM_NAME=Linux' in opts


# --- toolchain discovery ---

def test_the_toolchain_is_found_in_a_plain_bin_layout(tmp_path):
    """A distro cross package (apt install gcc-aarch64-linux-gnu) puts the compiler straight in bin/."""
    (tmp_path / 'bin').mkdir(parents=True)
    (tmp_path / 'bin' / f'aarch64-linux-gnu-gcc{executable_extension()}').write_text('')
    raspi = _raspi('arm64')
    raspi._search_paths = lambda: [str(tmp_path)]
    raspi.init_default()
    assert raspi.compiler_prefix() == f'{tmp_path}/bin/aarch64-linux-gnu-'


def test_the_toolchain_is_found_in_the_legacy_broadcom_layout(tmp_path):
    """The old Broadcom `tools` repo nests the 32-bit toolchain under arm-bcm2708/<triple>/."""
    nested = tmp_path / 'arm-bcm2708' / 'arm-linux-gnueabihf' / 'bin'
    nested.mkdir(parents=True)
    (nested / f'arm-linux-gnueabihf-gcc{executable_extension()}').write_text('')
    raspi = _raspi('arm')
    raspi._search_paths = lambda: [str(tmp_path)]
    raspi.init_default()
    assert raspi.compiler_prefix().endswith('arm-bcm2708/arm-linux-gnueabihf/bin/arm-linux-gnueabihf-')


def test_a_missing_toolchain_raises_instead_of_falling_back_to_the_host(tmp_path):
    """A silent fallback would build with the HOST gcc and quietly produce x86 binaries."""
    raspi = _raspi('arm64')
    raspi._search_paths = lambda: [str(tmp_path)]
    with pytest.raises(EnvironmentError, match='aarch64-linux-gnu-gcc'):
        raspi.init_default()


# --- install-raspi, apt only, so a Linux host only ---

@pytest.mark.parametrize('arg,arch', [('install-raspi', 'arm64'), ('install-raspi32', 'arm')])
def test_the_install_arg_queues_the_right_arch(arg, arch):
    assert BuildConfig([arg]).convenient_install == [f'raspi-{arch}']


@pytest.mark.linux_host
@pytest.mark.parametrize('arch,triple,build_cmd', [
    ('arm64', 'aarch64-linux-gnu', 'mama build raspi'),
    ('arm', 'arm-linux-gnueabihf', 'mama build raspi32'),
])
def test_install_raspi_apt_installs_the_cross_triple(arch, triple, build_cmd, monkeypatch, capsys):
    config = BuildConfig([])
    monkeypatch.setattr(config, 'get_distro_info', lambda: ('ubuntu', 24, 4))
    ran = []
    monkeypatch.setattr('mama.build_config.execute', ran.append)
    monkeypatch.setattr('mama.build_config.execute_piped', lambda *a, **k: '13.2.0')
    monkeypatch.setattr(os.path, 'exists', lambda p: p == f'/usr/bin/{triple}-gcc')
    config.install_raspi(arch)
    assert f'sudo apt-get install -y gcc-{triple} g++-{triple}' in ran
    assert build_cmd in capsys.readouterr().out   # tells the user what to run next


@pytest.mark.linux_host
def test_install_raspi_fails_loudly_when_apt_did_not_deliver(monkeypatch):
    """apt can 'succeed' with an unknown package. Without this the next build silently uses host gcc."""
    config = BuildConfig([])
    monkeypatch.setattr(config, 'get_distro_info', lambda: ('ubuntu', 24, 4))
    monkeypatch.setattr('mama.build_config.execute', lambda *a, **k: 0)
    monkeypatch.setattr(os.path, 'exists', lambda p: False)
    with pytest.raises(RuntimeError, match='aarch64-linux-gnu-gcc is still missing'):
        config.install_raspi('arm64')


@pytest.mark.linux_host
def test_install_raspi_rejects_an_unknown_arch():
    with pytest.raises(ValueError, match='Unsupported raspi arch'):
        BuildConfig([]).install_raspi('riscv')


# --- a distro cross package has no sysroot dir ---

def test_a_distro_cross_package_passes_no_sysroot(tmp_path):
    """Debian's gcc-aarch64-linux-gnu has no <triple>/sysroot. Passing one that is not there makes
    every compile fail on missing system headers."""
    (tmp_path / 'bin').mkdir()
    raspi = _raspi('arm64')
    raspi.init_toolchain(str(tmp_path))
    assert raspi.get_sysroot() == '' and raspi.get_includes() == []
    flags = {}
    raspi.get_cxx_flags(lambda f, v='': flags.__setitem__(f, v))
    assert '--sysroot' not in flags and flags['-march'] == 'armv8-a'


def test_a_standalone_toolchain_still_passes_its_sysroot(tmp_path):
    (tmp_path / 'bin').mkdir()
    (tmp_path / 'aarch64-linux-gnu' / 'sysroot').mkdir(parents=True)
    raspi = _raspi('arm64')
    raspi.init_toolchain(str(tmp_path))
    assert raspi.get_sysroot() == f'{tmp_path}/aarch64-linux-gnu/sysroot'
