from __future__ import annotations
from typing import TYPE_CHECKING
from typing import Callable
from .generic_yocto import GenericYocto


if TYPE_CHECKING:
    from ..build_config import BuildConfig


class Oclea(GenericYocto):
    def __init__(self, config: BuildConfig):
        ## Ambarella CV25 by Oclea is a Cortex-A53 based SoC with a HW Encoder.
        super().__init__('oclea', 'OCLEA', config)


    def init_toolchain(self, toolchain_dir=None, toolchain_file=None):
        paths = []
        if toolchain_dir: paths += [ toolchain_dir ]
        # this is the primary search path for Linux cross-builds:
        paths += [ '/opt/oclea/1.0' ]
        # these are generic ones:
        paths += [ 'oclea-toolchain', 'oclea-toolchain/toolchain' ]

        self._yocto_toolchain_init(toolchain_dir, toolchain_file,
                                   paths=paths,
                                   envs=['OCLEA_HOME', 'OCLEA_SDK'],
                                   compiler_name='usr/bin/aarch64-oclea-linux/aarch64-oclea-linux-gcc',
                                   sdk_name='x86_64-ocleasdk-linux',
                                   sysroot_name='cortexa53-oclea-linux',
                                   default_toolchain='aarch64_oclea_toolchain.cmake')


    def get_cxx_flags(self, add_flag: Callable[[str,str], None]):
        add_flag('-march', 'armv8-a')
        add_flag('-mcpu', 'cortex-a53+crypto')
        add_flag('-mlittle-endian')
        self._add_common_cxx_flags(add_flag)

