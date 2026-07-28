from __future__ import annotations
from typing import Callable
from .generic_yocto import GenericYocto


class Imx8mp(GenericYocto):
    """NXP i.MX8M Plus. A Cortex-A53 based SoC with an integrated NPU, supported by the Yocto SDKs
    that NXP and third parties like IMD Tec ship."""
    name = 'imx8mp'
    host_triple = 'aarch64-poky-linux'

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
        super().get_cxx_flags(add_flag)
