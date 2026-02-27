from __future__ import annotations
from typing import TYPE_CHECKING
from typing import Callable
from .generic_yocto import GenericYocto


if TYPE_CHECKING:
    from ..build_config import BuildConfig


class Xilinx(GenericYocto):
    def __init__(self, config: BuildConfig):
        ## Xilinx Zynq UltraScale+ MPSoC
        super().__init__('xilinx', 'XILINX', config)


    def init_toolchain(self, toolchain_dir=None, toolchain_file=None):
        paths = []
        if toolchain_dir: paths += [ toolchain_dir ]
        # this is the primary search path for Linux cross-builds:
        paths += [ '/opt/petalinux/toolchain' ]
        # these are generic ones:
        paths += [ 'xilinx-toolchain', 'xilinx-toolchain/toolchain' ]

        compiler = 'usr/bin/aarch64-xilinx-linux/aarch64-xilinx-linux-gcc'

        self._yocto_toolchain_init(toolchain_dir, toolchain_file,
                                   paths=paths,
                                   envs=['XILINX_HOME', 'XILINX_SDK'],
                                   compiler_name=compiler,
                                   sdk_name='x86_64-petalinux-linux',
                                   sysroot_name='cortexa72-cortexa53-xilinx-linux',
                                   default_toolchain='aarch64_xilinx_toolchain.cmake')


    def get_cxx_flags(self, add_flag: Callable[[str,str], None]):
        # Match PetaLinux SDK environment-setup flags
        add_flag('-mcpu', 'cortex-a72.cortex-a53+crc')
        add_flag('-mbranch-protection', 'standard')
        self._add_common_cxx_flags(add_flag)

