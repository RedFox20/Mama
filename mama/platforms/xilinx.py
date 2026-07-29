from __future__ import annotations
from .generic_yocto import GenericYocto


class Xilinx(GenericYocto):
    """Xilinx Zynq UltraScale+ MPSoC, built against a PetaLinux SDK."""
    name = 'xilinx'
    host_triple = 'aarch64-xilinx-linux'
    search_paths = ('/opt/petalinux/toolchain', 'xilinx-toolchain', 'xilinx-toolchain/toolchain')
    search_envs = ('XILINX_HOME', 'XILINX_SDK')
    compiler_name = 'usr/bin/aarch64-xilinx-linux/aarch64-xilinx-linux-gcc'
    sdk_name = 'x86_64-petalinux-linux'
    sysroot_name = 'cortexa72-cortexa53-xilinx-linux'
    default_toolchain = 'aarch64_xilinx_toolchain.cmake'
    # match the PetaLinux SDK environment-setup flags
    cpu_flags = {'-mcpu': 'cortex-a72.cortex-a53+crc', '-mbranch-protection': 'standard'}
