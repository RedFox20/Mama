from __future__ import annotations
from typing import TYPE_CHECKING

from mama.cmake_configure import cross_system_opts, use_toolchain_file

if TYPE_CHECKING:
    from ..build_config import BuildConfig
    from ..build_target import BuildTarget


class Ios:
    """iOS cross build. Always arm64 device builds; the simulator is not supported."""
    def __init__(self, config: BuildConfig):
        self.config = config

    def get_cmake_build_opts(self, target: BuildTarget) -> list:
        config = self.config
        opts = ['IOS_PLATFORM=OS'] + cross_system_opts(config, 'Darwin', 'arm64') + [
            'CMAKE_XCODE_EFFECTIVE_PLATFORMS=-iphoneos',
            'CMAKE_OSX_ARCHITECTURES=arm64', # ALWAYS ARM64
            'CMAKE_OSX_SYSROOT=iphoneos',
        ]
        if target.cmake_ios_toolchain:
            toolchain = target.source_dir(target.cmake_ios_toolchain)
            opts.append(use_toolchain_file(config, toolchain))
            config.announce_once('toolchain', f'Toolchain: {toolchain}')
        return opts
