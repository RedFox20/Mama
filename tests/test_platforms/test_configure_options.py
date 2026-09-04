"""Pins the exact cmake configure options and compiler flags every platform emits."""
import pytest

from testutils import platform_cxx_flags, platform_target
from mama.buildsys.cmake import configure as cc
from mama.platforms.aarch64 import Aarch64
from mama.platforms.android import Android
from mama.platforms.imx8mp import Imx8mp
from mama.platforms.ios import Ios
from mama.platforms.linux import Linux
from mama.platforms.macos import Macos
from mama.platforms.mips import Mips
from mama.platforms.oclea import Oclea
from mama.platforms.raspi import Raspi
from mama.platforms.windows import Windows
from mama.platforms.xilinx import Xilinx


def _opts(tmp_path, platform_class, arch=None, **overrides):
    t, _ = platform_target(tmp_path, platform_class, arch, **overrides)
    return cc._platform_opts(t)


# --- cross platforms name the target system, never the host ---

@pytest.mark.parametrize('platform_class,arch,system,processor', [
    (Android, 'arm64', 'Android', 'aarch64'),
    (Android, 'arm',   'Android', 'armv7-a'),
    (Raspi,   'arm64', 'Linux',   'aarch64'),
    (Aarch64, 'arm64', 'Linux', 'aarch64'),
    (Raspi,   'arm',   'Linux',   'armv7-a'),
    (Mips,    'mipsel','Linux',   'mipsel'),
    (Oclea,   'arm64', 'Linux',   'aarch64'),
    (Imx8mp,  'arm64', 'Linux',   'aarch64'),
    (Xilinx,  'arm64', 'Linux',   'aarch64'),
    (Ios,     'arm64', 'Darwin',  'arm64'),  # Apple names it arm64 everywhere, never aarch64
])
def test_a_cross_platform_names_its_target_system(platform_class, arch, system, processor,
                                                  tmp_path, fake_toolchains):
    opts = _opts(tmp_path, platform_class, arch)
    assert f'CMAKE_SYSTEM_NAME={system}' in opts
    assert f'CMAKE_SYSTEM_PROCESSOR={processor}' in opts


@pytest.mark.parametrize('platform_class', [Linux, Macos])
def test_a_native_platform_names_no_target_system(platform_class, tmp_path):
    """CMAKE_SYSTEM_NAME on a native build makes cmake treat it as a cross build."""
    assert _opts(tmp_path, platform_class) == []


def test_msvc_only_overrides_the_toolset_for_x86(tmp_path):
    assert _opts(tmp_path, Windows, 'x64') == []
    assert _opts(tmp_path, Windows, 'x86') == ['CMAKE_GENERATOR_TOOLSET=host=x86']


# --- each platform's own option set ---

def test_android_passes_the_ndk_abi_and_toolchain_file(tmp_path, fake_toolchains):
    opts = _opts(tmp_path, Android, 'arm64')
    assert 'ANDROID_ABI=arm64-v8a' in opts and 'ANDROID_ARCH=ARM64' in opts
    assert 'ANDROID_STL=c++_shared' in opts and 'ANDROID_NATIVE_API_LEVEL=android-29' in opts
    assert f'CMAKE_TOOLCHAIN_FILE="{fake_toolchains["ndk"]}/build/cmake/android.toolchain.cmake"' in opts


def test_android_armv7_switches_the_abi(tmp_path, fake_toolchains):
    assert 'ANDROID_ABI=armeabi-v7a' in _opts(tmp_path, Android, 'arm')


def test_android_passes_the_make_program_exactly_once(tmp_path, fake_toolchains):
    """It used to come from the platform's own list AND the generic option builder."""
    t, _ = platform_target(tmp_path, Android, 'arm64', ninja_path='/usr/bin/ninja', prefer_ninja=True)
    names = [o.split('=')[0] for o in cc._default_options(t)]
    assert names.count('CMAKE_MAKE_PROGRAM') <= 1


@pytest.mark.parametrize('platform_class,define', [(Raspi, 'RASPI'), (Mips, 'MIPS'),
                                                   (Aarch64, 'AARCH64_LINUX'),
                                                   (Oclea, 'OCLEA'), (Imx8mp, 'IMX8MP'), (Xilinx, 'XILINX')])
def test_a_board_announces_itself_to_the_project(platform_class, define, tmp_path, fake_toolchains):
    assert f'{define}=TRUE' in _opts(tmp_path, platform_class)


@pytest.mark.parametrize('platform_class,mode', [(Raspi, 'NEVER'), (Aarch64, 'NEVER'), (Mips, 'ONLY')])
def test_the_find_root_program_mode_is_per_platform(platform_class, mode, tmp_path, fake_toolchains):
    """MIPS ships its own binutils and must find them in the toolchain. A raspi distro cross package
    ships none, so cmake has to take the compiler tools mama named."""
    opts = _opts(tmp_path, platform_class)
    assert f'CMAKE_FIND_ROOT_PATH_MODE_PROGRAM={mode}' in opts
    assert 'CMAKE_FIND_ROOT_PATH_MODE_LIBRARY=ONLY' in opts
    assert 'CMAKE_FIND_ROOT_PATH_MODE_INCLUDE=ONLY' in opts


@pytest.mark.parametrize('platform_class,name', [(Oclea, 'aarch64_oclea_toolchain.cmake'),
                                                 (Xilinx, 'aarch64_xilinx_toolchain.cmake')])
def test_a_yocto_board_uses_the_toolchain_file_its_sdk_ships(platform_class, name, tmp_path, fake_toolchains):
    root = fake_toolchains[platform_class.name]
    assert f'CMAKE_TOOLCHAIN_FILE="{root}/{name}"' in _opts(tmp_path, platform_class)


def test_ios_builds_for_the_device_not_the_simulator(tmp_path):
    opts = _opts(tmp_path, Ios)
    assert 'IOS_PLATFORM=OS' in opts and 'CMAKE_OSX_ARCHITECTURES=arm64' in opts
    assert 'CMAKE_OSX_SYSROOT=iphoneos' in opts


# --- compiler flags ---

@pytest.mark.parametrize('platform_class,arch,march', [
    (Android, 'arm64', 'armv8-a'), (Android, 'arm', 'armv7-a'),
    (Raspi, 'arm64', 'armv8-a'),   (Raspi, 'arm', 'armv7-a'),
    (Aarch64, 'arm64', 'armv8-a'),
    (Oclea, 'arm64', 'armv8-a'),   (Imx8mp, 'arm64', 'armv8-a'),
])
def test_the_march_follows_the_target_arch(platform_class, arch, march, tmp_path, fake_toolchains):
    assert platform_cxx_flags(tmp_path, platform_class, arch)['-march'] == march


@pytest.mark.parametrize('platform_class', [Android, Oclea, Imx8mp, Raspi, Aarch64])
def test_a_target_march_pin_beats_the_platform_default(platform_class, tmp_path, fake_toolchains):
    flags = platform_cxx_flags(tmp_path, platform_class, 'arm64', target_march={'arm64': 'armv8.2-a'})
    assert flags['-march'] == 'armv8.2-a'


@pytest.mark.parametrize('platform_class,fpu', [(Android, 'neon'), (Raspi, 'neon-vfpv4')])
def test_the_32bit_arm_build_declares_an_fpu(platform_class, fpu, tmp_path, fake_toolchains):
    """armv7-a alone declares no FPU, and a hard-float triple then refuses to compile at all."""
    assert platform_cxx_flags(tmp_path, platform_class, 'arm')['-mfpu'] == fpu


@pytest.mark.linux_host
def test_xilinx_matches_the_petalinux_sdk_flags(tmp_path, fake_toolchains):
    flags = platform_cxx_flags(tmp_path, Xilinx)
    assert flags['-mcpu'] == 'cortex-a72.cortex-a53+crc' and flags['-mbranch-protection'] == 'standard'


@pytest.mark.parametrize('platform_class', [Oclea, Imx8mp])
def test_a_yocto_board_defines_itself_and_yocto_linux(platform_class, tmp_path, fake_toolchains):
    flags = platform_cxx_flags(tmp_path, platform_class)
    assert flags[f'-D{platform_class.name.upper()}'] == '1' and flags['-DYOCTO_LINUX'] == '1'


@pytest.mark.linux_host
def test_a_yocto_board_drops_libraries_it_never_calls(tmp_path, fake_toolchains):
    """-Wl,--as-needed: an embedded binary that links unused libraries is bloated and can break
    at runtime on a resource-constrained device."""
    t, _ = platform_target(tmp_path, Imx8mp)
    cc._default_options(t)
    assert '-Wl,--as-needed' in t.cmake_ldflags


@pytest.mark.linux_host
def test_mips_defines_itself(tmp_path, fake_toolchains):
    assert platform_cxx_flags(tmp_path, Mips, 'mipsel')['-DMIPS'] == '1'


@pytest.mark.linux_host
@pytest.mark.parametrize('tool', ['ar', 'readelf', 'strip', 'ranlib'])
def test_mips_names_the_cross_binutils_it_ships(tool, tmp_path, fake_toolchains):
    """FIND_ROOT_PATH_MODE_PROGRAM=ONLY restricts cmake's own search to the target root, and mama sets
    no find root for MIPS, so the tools have to be named or cmake can end up with the host's."""
    opts = _opts(tmp_path, Mips, 'mipsel')
    assert any(o.startswith(f'CMAKE_{tool.upper()}=') and o.endswith(f'-{tool}') for o in opts)


@pytest.mark.parametrize('platform_class,stdlib', [(Macos, 'libc++'), (Ios, 'libc++'), (Linux, '')])
def test_only_the_apple_and_clang_platforms_pick_a_stdlib(platform_class, stdlib, tmp_path):
    assert platform_cxx_flags(tmp_path, platform_class).get('-stdlib', '') == stdlib


def test_linux_clang_picks_libcxx_and_can_be_switched(tmp_path):
    assert platform_cxx_flags(tmp_path, Linux, clang=True)['-stdlib'] == 'libc++'
    assert platform_cxx_flags(tmp_path, Linux, clang=True, clang_stdlib='libstdc++')['-stdlib'] == 'libstdc++'


def test_ios_pins_the_deployment_target(tmp_path):
    flags = platform_cxx_flags(tmp_path, Ios, ios_version='16.0')
    assert flags['-miphoneos-version-min'] == '16.0' and '-arch arm64' in flags


def test_msvc_uses_its_own_flag_syntax(tmp_path):
    flags = platform_cxx_flags(tmp_path, Windows)
    assert '/EHsc' in flags and '/MP' in flags and flags['-DWIN32'] == '1'
