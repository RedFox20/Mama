from __future__ import annotations
from .generic_yocto import GenericYocto


class Imx8mp(GenericYocto):
    """NXP i.MX8M Plus. A Cortex-A53 based SoC with an integrated NPU, supported by the Yocto SDKs
    that NXP and third parties like IMD Tec ship."""
    name = 'imx8mp'
    host_triple = 'aarch64-poky-linux'
    search_paths = ('/opt/imdt-imx-xwayland/5.0.4', '/opt/imx8mp-sdk', 'imx8mp-toolchain')
    search_envs = ('IMX8MP_SDK_HOME',)
    compiler_name = 'usr/bin/aarch64-poky-linux/aarch64-poky-linux-gcc'
    sdk_name = 'x86_64-pokysdk-linux'
    sysroot_name = 'cortexa53-crypto-poky-linux'
    default_toolchain = 'sysroots/x86_64-pokysdk-linux/usr/share/cmake/' \
                        'cortexa53-crypto-poky-linux-toolchain.cmake'
    cpu_flags = {'-march': 'armv8-a', '-mcpu': 'cortex-a53+crypto', '-mlittle-endian': ''}
