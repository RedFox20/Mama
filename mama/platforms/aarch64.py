from __future__ import annotations

from .gnu_cross import GnuCross


class Aarch64(GnuCross):
    """Generic 64-bit ARM Linux, built with the standard `aarch64-linux-gnu` GNU cross toolchain.

    The target for an embedded board that ships no SDK of its own: a Yocto or Buildroot image whose
    vendor never published a sysroot, so the build uses the distro cross toolchain and the project
    links its binaries statically. A board with a real SDK belongs on GenericYocto, and a Raspberry
    Pi on Raspi - both describe more than this does.

    `aarch64` used to be an alias of the arm64 ARCH, so `mama build android aarch64` pinned the
    android build to arm64. It names THIS PLATFORM now, and naming it after another platform raises
    rather than switching: a silent switch hands back a linux build that looks like the android one
    the user asked for. Spell an arch pin `arm64`, which the help always documented anyway.

    A board that needs a sysroot of its own names a cmake toolchain file through the
    `cmake_aarch64_toolchain` attribute on its BuildTarget. A `-mcpu` needs no file: declare it with
    `add_platform_cxx_flags(aarch64='-mcpu=cortex-a53')`.
    """
    name = 'aarch64'
    display_name = 'AArch64 Linux'
    default_arch = 'arm64'
    # AARCH64_LINUX, not AARCH64: the generated mama.cmake matches the arm64 ARCH with a regex whose
    # own text is `(aarch64)|(AARCH64)|...`, and CPU-detection code everywhere sets a plain AARCH64.
    # A consumer setting that for its own reasons would then select this platform's build dir.
    platform_define = 'AARCH64_LINUX'
    toolchain_override_attr = 'cmake_aarch64_toolchain'
    # the distro cross package is current gcc, unlike the vendor SDKs that Raspi has to keep working with
    cxx20_flag = 'c++20'

    triples = {'arm64': 'aarch64-linux-gnu'}
    marches = {'arm64': 'armv8-a'}
    search_envs = ('AARCH64_HOME',)
    linux_paths = ('/usr/local', '/opt/aarch64-linux-gnu', '/usr')
