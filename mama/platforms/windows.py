from __future__ import annotations
import os
from platform import version as _os_version  # stdlib platform, NOT mama.platforms.platform
from .platform import Platform


class Windows(Platform):
    """MSVC on Windows. Not a cross build: the toolset and the Windows SDK pick the compiler, so
    mama names no compiler path and BuildConfig resolves the toolset through vswhere instead."""
    name = 'windows'
    cli_aliases = ('msvc',)
    system_name = 'Windows'
    build_system = 'visualstudio'
    supported_arches = ('x86', 'x64', 'arm', 'arm64')
    build_dirs = {'x64': 'windows', 'x86': 'windows32', 'arm64': 'winarm', 'arm': 'winarm32'}

    def distro_version(self) -> tuple:
        version = _os_version().split('.') + ['0']
        return (self.name, int(version[0]), int(version[1]))


    def compiler_version_tag(self) -> str:
        toolset = os.path.basename(self.config.get_msvc_tools_path().rstrip('\\//'))
        return f'msvc{toolset.split(".")[0]}'


    def exe_suffix(self) -> str:
        return '.exe'


    def lib_extensions(self) -> tuple:
        return ('.lib',)


    def debugger(self) -> str:
        return ''  # the test exe runs directly, there is no batch-mode debugger to wrap it
