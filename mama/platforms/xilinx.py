from __future__ import annotations
from typing import Callable
from .generic_yocto import GenericYocto


class Xilinx(GenericYocto):
    """Xilinx Zynq UltraScale+ MPSoC, built against a PetaLinux SDK."""
    name = 'xilinx'
    host_triple = 'aarch64-xilinx-linux'

    def init_toolchain(self, toolchain_dir=None, toolchain_file=None):
        paths = []
        if toolchain_dir: paths += [ toolchain_dir ]
        # this is the primary search path for Linux cross-builds:
        paths += [ '/opt/petalinux/toolchain' ]
        # these are generic ones:
        paths += [ 'xilinx-toolchain', 'xilinx-toolchain/toolchain' ]

        self._yocto_toolchain_init(toolchain_dir, toolchain_file,
                                   paths=paths,
                                   envs=['XILINX_HOME', 'XILINX_SDK'],
                                   compiler_name='usr/bin/aarch64-xilinx-linux/aarch64-xilinx-linux-gcc',
                                   sdk_name='x86_64-petalinux-linux',
                                   sysroot_name='cortexa72-cortexa53-xilinx-linux',
                                   default_toolchain='aarch64_xilinx_toolchain.cmake')


    def get_cxx_flags(self, add_flag: Callable[[str,str], None]):
        # Match PetaLinux SDK environment-setup flags
        add_flag('-mcpu', 'cortex-a72.cortex-a53+crc')
        add_flag('-mbranch-protection', 'standard')
        super().get_cxx_flags(add_flag)
