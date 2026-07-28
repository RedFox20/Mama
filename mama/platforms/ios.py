from __future__ import annotations
from typing import TYPE_CHECKING, Callable
import os

from .platform import Platform
from mama.cmake_configure import cross_system_opts, use_toolchain_file

if TYPE_CHECKING:
    from ..build_target import BuildTarget


class Ios(Platform):
    """iOS cross build. Always arm64 device builds. The simulator is not supported."""
    name = 'ios'
    system_name = 'Darwin'
    build_system = 'xcode'
    is_cross = True
    is_host_runnable = False  # an arm64 device binary does not run on the mac that built it
    default_arch = 'arm64'
    supported_arches = ('arm64',)

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


    def get_cmake_build_opts(self, target: BuildTarget) -> list:
        config = self.config
        opts = ['IOS_PLATFORM=OS'] + cross_system_opts(config, self.system_name, self.system_processor()) + [
            'CMAKE_XCODE_EFFECTIVE_PLATFORMS=-iphoneos',
            'CMAKE_OSX_ARCHITECTURES=arm64', # ALWAYS ARM64
            'CMAKE_OSX_SYSROOT=iphoneos',
        ]
        if target.cmake_ios_toolchain:
            toolchain = target.source_dir(target.cmake_ios_toolchain)
            opts.append(use_toolchain_file(config, toolchain))
            config.announce_once('toolchain', f'Toolchain: {toolchain}')
        return opts
