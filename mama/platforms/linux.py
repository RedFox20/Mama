from __future__ import annotations
from typing import Callable
from .platform import Platform, native_march
from ..utils.system import console, Color


class Linux(Platform):
    """A native Linux build with the host gcc or clang. 32-bit arm is not supported here, because
    an arm Linux build is either a raspi or a Yocto board, and both have their own platform."""
    name = 'linux'
    supported_arches = ('x86', 'x64', 'arm64')
    build_dirs = {'x64': 'linux', 'x86': 'linux32', 'arm64': 'linuxarm'}
    syslib_is_searchable = True  # a syslib is a real file to find under /usr/lib

    def validate_arch(self, arch: str):
        if arch == 'arm':
            raise RuntimeError(f'Unsupported arch={arch} on linux platform!' + \
                               ' Use raspi32 for a 32-bit ARM Linux target.')
        super().validate_arch(arch)


    def distro_version(self) -> tuple:
        import distro  # only installed where it can work, so keep it out of the module import
        try:
            dist = distro.info()
            return (dist['id'], int(dist['version_parts']['major']), int(dist['version_parts']['minor']))
        except Exception as err:
            console(f'Failed to parse linux distro; falling back to Ubuntu 16.04 LTS: {err}', color=Color.RED)
            return ('ubuntu', 16, 4)


    def cxx_stdlib(self) -> str:
        # config.use_gcc_stdlib_for_clang() switches this to libstdc++ to link GNU-built prebuilts
        return self.config.clang_stdlib if self.config.clang else ''


    def get_cxx_flags(self, add_flag: Callable[[str, str], None]):
        add_flag('-march', native_march(self.arch()))
        super().get_cxx_flags(add_flag)
