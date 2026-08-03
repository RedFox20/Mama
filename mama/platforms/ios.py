from __future__ import annotations
from typing import Callable
import os

from .platform import Platform
from .toolchain import Toolchain


class Ios(Platform):
    """iOS cross build. Always arm64 device builds. The simulator is not supported."""
    name = 'ios'
    system_name = 'Darwin'
    build_system = 'xcode'
    is_cross = True
    is_host_runnable = False  # an arm64 device binary does not run on the mac that built it
    default_arch = 'arm64'
    supported_arches = ('arm64',)
    toolchain_override_attr = 'cmake_ios_toolchain'
    syslib_is_framework = True
    ide_project_ext = ('.xcodeproj',)
    ide_project_is_dir = True
    ide_open_command = 'open'

    def system_processor(self) -> str:
        return 'arm64'  # Apple names it arm64 everywhere, never aarch64


    def distro_version(self) -> tuple:
        version = self.config.ios_version.split('.') + ['0']
        return (self.name, int(version[0]), int(version[1]))


    def compiler_version_tag(self) -> str:
        return self.config.ios_version


    def cxx_stdlib(self) -> str:
        return 'libc++'


    def get_cxx_flags(self, add_flag: Callable[[str, str], None]):
        add_flag('-arch arm64')
        add_flag('-miphoneos-version-min', self.config.ios_version)
        super().get_cxx_flags(add_flag)


    def inject_env(self):
        os.environ['IPHONEOS_DEPLOYMENT_TARGET'] = self.config.ios_version


    def lib_extensions(self) -> tuple:
        return ('.a', '.dylib', '.framework')


    def debugger(self) -> str:
        return ''  # tests cannot run on the host, so there is nothing to debug


    def _build_toolchain(self) -> Toolchain:
        # Xcode SDK selection has no build-system-neutral form, so it goes through the escape hatch
        return Toolchain(system_name=self.system_name, system_processor=self.system_processor(),
                         extra_opts=('IOS_PLATFORM=OS', 'CMAKE_XCODE_EFFECTIVE_PLATFORMS=-iphoneos',
                                     'CMAKE_OSX_ARCHITECTURES=arm64',  # ALWAYS ARM64
                                     'CMAKE_OSX_SYSROOT=iphoneos'))
