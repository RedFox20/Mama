from __future__ import annotations
from typing import TYPE_CHECKING

from mama.cmake_configure import cross_system_opts, use_toolchain_file

if TYPE_CHECKING:
    from ..build_config import BuildConfig
    from ..build_target import BuildTarget


class Raspi:
    """Raspberry Pi cross build. Always ARMv7, so the arch is fixed rather than read from config.arch."""
    def __init__(self, config: BuildConfig):
        self.config = config

    def get_cmake_build_opts(self, target: BuildTarget) -> list:
        config = self.config
        opts = ['RASPI=TRUE'] + cross_system_opts(config, 'Linux', 'armv7-a') + [
            'CMAKE_SYSTEM_VERSION=1',
            'CMAKE_FIND_ROOT_PATH_MODE_PROGRAM=NEVER', # Use our definitions for compiler tools
            'CMAKE_FIND_ROOT_PATH_MODE_LIBRARY=ONLY', # Search for libs and headers in the target dirs only
            'CMAKE_FIND_ROOT_PATH_MODE_INCLUDE=ONLY',
        ]
        if target.cmake_raspi_toolchain:
            toolchain = target.source_dir(target.cmake_raspi_toolchain)
            opts.append(use_toolchain_file(config, toolchain))
            config.announce_once('toolchain', f'Toolchain: {toolchain}')
        return opts
