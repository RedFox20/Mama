from __future__ import annotations
from .generic_yocto import GenericYocto


class Oclea(GenericYocto):
    """Ambarella CV25 by Oclea. A Cortex-A53 based SoC with a hardware video encoder."""
    name = 'oclea'
    host_triple = 'aarch64-oclea-linux'
    search_paths = ('/opt/oclea/1.0', 'oclea-toolchain', 'oclea-toolchain/toolchain')
    search_envs = ('OCLEA_HOME', 'OCLEA_SDK')
    compiler_name = 'usr/bin/aarch64-oclea-linux/aarch64-oclea-linux-gcc'
    sdk_name = 'x86_64-ocleasdk-linux'
    sysroot_name = 'cortexa53-oclea-linux'
    default_toolchain = 'aarch64_oclea_toolchain.cmake'
    cpu_flags = {'-march': 'armv8-a', '-mcpu': 'cortex-a53+crypto', '-mlittle-endian': ''}
