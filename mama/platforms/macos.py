from __future__ import annotations
from .platform import Platform


class Macos(Platform):
    """A native macOS build with the Xcode clang. Apple dropped 32-bit, and arm64 is the default
    since the M1, so only x64 and arm64 remain."""
    name = 'macos'
    system_name = 'Darwin'
    default_arch = 'arm64'
    supported_arches = ('x64', 'arm64')
    build_dirs = {'x64': 'macos', 'arm64': 'macosarm'}

    def lib_extensions(self) -> tuple:
        return ('.a', '.dylib', '.bundle')

    def debugger(self) -> str:
        return 'lldb'
