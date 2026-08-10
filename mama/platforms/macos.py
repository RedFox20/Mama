from __future__ import annotations
import os
from functools import lru_cache
from .platform import Platform, native_march, host_arch


@lru_cache(maxsize=1)
def rosetta_installed() -> bool:
    """True when Rosetta 2 is on this Mac. Apple ships it as an optional install, and without it an
    x64 binary does not run on Apple silicon."""
    return any(os.path.exists(p) for p in ('/Library/Apple/usr/libexec/oah',
                                           '/Library/Apple/usr/share/rosetta'))


class Macos(Platform):
    """A native macOS build with the Xcode clang. Apple dropped 32-bit, and arm64 is the default
    since the M1, so only x64 and arm64 remain."""
    name = 'macos'
    system_name = 'Darwin'
    build_system = 'xcode'
    default_arch = 'arm64'
    supported_arches = ('x64', 'arm64')
    build_dirs = {'x64': 'macos', 'arm64': 'macosarm'}
    also_runs = {'arm64': ('x64',)}  ## Rosetta 2 runs an x64 tool on Apple silicon, when installed
    syslib_is_framework = True
    ide_project_ext = ('.xcodeproj',)
    ide_project_is_dir = True
    ide_open_command = 'open'

    def runs_on_host(self, arch: str) -> bool:
        """Rosetta 2 is an optional install, so an x64 tool runs on Apple silicon only where it is."""
        if arch == 'x64' and host_arch() == 'arm64': return rosetta_installed()
        return super().runs_on_host(arch)


    def distro_version(self) -> tuple:
        version = self.config.macos_version.split('.') + ['0']
        return (self.name, int(version[0]), int(version[1]))


    def compiler_version_tag(self) -> str:
        return self.config.macos_version


    def cxx_stdlib(self) -> str:
        return 'libc++'


    def default_march(self) -> str:
        return native_march(self.arch())


    def inject_env(self):
        os.environ['MACOSX_DEPLOYMENT_TARGET'] = self.config.macos_version


    def lib_extensions(self) -> tuple:
        return ('.a', '.dylib', '.bundle')


    def debugger(self) -> str:
        return 'lldb'
