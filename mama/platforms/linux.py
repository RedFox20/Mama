from __future__ import annotations
from .platform import Platform


class Linux(Platform):
    """A native Linux build with the host gcc or clang. 32-bit arm is not supported here, because
    an arm Linux build is either a raspi or a Yocto board, and both have their own platform."""
    name = 'linux'
    supported_arches = ('x86', 'x64', 'arm64')
    build_dirs = {'x64': 'linux', 'x86': 'linux32', 'arm64': 'linuxarm'}

    def validate_arch(self, arch: str):
        if arch == 'arm':
            raise RuntimeError(f'Unsupported arch={arch} on linux platform! Build with android instead')
        super().validate_arch(arch)
