from __future__ import annotations
from typing import TYPE_CHECKING
from typing import Callable
from .generic_yocto import GenericYocto


if TYPE_CHECKING:
    from ..build_config import BuildConfig


class Imx8mp(GenericYocto):
    def __init__(self, config: BuildConfig):
        # NXP i.MX8M Plus (imx8mp) is a Cortex-A53 based SoC with integrated NPU, 
        # and is supported by Yocto SDKs provided by NXP and 3rd parties like IMD Tec
        super().__init__('imx8mp', 'IMX8MP', config)


    def init_toolchain(self, toolchain_dir=None, toolchain_file=None):
        paths = []
        if toolchain_dir: paths += [ toolchain_dir ]
        paths += [ '/opt/imdt-imx-xwayland/5.0.4' ]
        paths += [ '/opt/imx8mp-sdk' ]
        paths += [ 'imx8mp-toolchain' ]

        # /opt/imdt-imx-xwayland/5.0.4/sysroots/x86_64-pokysdk-linux/usr/bin/aarch64-poky-linux/aarch64-poky-linux-gcc
        compiler = 'usr/bin/aarch64-poky-linux/aarch64-poky-linux-gcc'
        default_toolchain = 'sysroots/x86_64-pokysdk-linux/usr/share/cmake/cortexa53-crypto-poky-linux-toolchain.cmake'

        self._yocto_toolchain_init(toolchain_dir, toolchain_file,
                                   paths=paths,
                                   envs=['IMX8MP_SDK_HOME'],
                                   compiler_name=compiler,
                                   sdk_name='x86_64-pokysdk-linux',
                                   sysroot_name='cortexa53-crypto-poky-linux',
                                   default_toolchain=default_toolchain) 


    def get_cxx_flags(self, add_flag: Callable[[str,str], None]):
        add_flag('-march', 'armv8-a')
        add_flag('-mcpu', 'cortex-a53+crypto')
        add_flag('-mlittle-endian')
        self._add_common_cxx_flags(add_flag)

