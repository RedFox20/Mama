from __future__ import annotations

from .gnu_cross import GnuCross


class Raspi(GnuCross):
    """Raspberry Pi cross build. A plain GNU cross toolchain, the way GnuCross describes: the distro
    cross package, SysGCC on Windows, or the legacy Broadcom `tools` checkout."""
    name = 'raspi'
    display_name = 'Raspberry PI'
    arch_aliases = {'raspi32': 'arm'}  # legacy ARMv7
    cxx20_flag = 'c++2a'  # these SDKs ship gcc older than the final C++20 name
    default_arch = 'arm64'  # every Pi since the 3 is ARMv8
    platform_define = 'RASPI'
    toolchain_override_attr = 'cmake_raspi_toolchain'
    build_dirs = {'arm64': 'raspi', 'arm': 'raspi32'}

    # The triple drives the compiler name, the sysroot and the include dir, so a new arch is one entry here.
    triples = {'arm64': 'aarch64-linux-gnu', 'arm': 'arm-linux-gnueabihf'}
    marches = {'arm64': 'armv8-a', 'arm': 'armv7-a'}
    # The gnueabihf triple defaults to hard float, so armv7-a must name an FPU. neon-vfpv4 is what the Pi 2 and 3 have.
    mfpus = {'arm': 'neon-vfpv4'}
    search_envs = ('RASPI_HOME', 'RASPBERRY_HOME')
    windows_paths = ('/SysGCC/raspberry',)
    linux_paths = ('/usr/bin/raspberry', '/usr/local/bin/raspberry', '/opt/raspberry', '/usr')


    def _layouts(self, root: str) -> list:
        """The legacy Broadcom `tools` repo nests the 32-bit toolchain under arm-bcm2708/<triple>/.
        Every other toolchain puts its bin/ dir straight in the root."""
        return [root, f'{root}/arm-bcm2708/{self.triple()}']


SUPPORTED_ARCHES = Raspi.supported_arches


def triple_for_arch(arch: str) -> str:
    """The GNU triple mama cross-compiles with for a raspi arch, eg arm64 -> aarch64-linux-gnu."""
    return Raspi.triple_for(arch)
