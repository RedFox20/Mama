import os
from typing import Callable
from mama.utils.system import System, console
from mama.util import path_join, read_lines_from

class Mips:
    def __init__(self, config):
        self.name = 'mips'
        self.toolchain_major = 1
        self.toolchain_minor = 0
        self.config = config
        self.toolchain_dir = None
        self.toolchain_file = None
        self.supported_arches = ['mips', 'mipsel', 'mips64', 'mips64el']
        self.mips_arch = 'mipsel' # prefer little endian mips
        self.gcc_prefix = '' # prefix to gcc binary
        self.libs_path = '' # toolchain lib/ path
        self.include_path = '' # toolchain include/ path


    # returns the current path prefix where the compiler can be found
    # example: "/opt/mipsel-openwrt-linux/bin/mipsel-openwrt-linux-"
    def compiler_prefix(self):
        if not self.gcc_prefix: self.init_default()
        return self.gcc_prefix


    # forced includes that should be added to compiler flags as -I paths
    def includes(self):
        return []


    def init_default(self):
        if not self.gcc_prefix:
            self.init_toolchain(self.mips_arch)


    def init_toolchain(self, arch, toolchain_dir=None, toolchain_file=None):
        if arch not in self.supported_arches:
            raise RuntimeError(f'Unsupported MIPS arch: {arch}')
        if toolchain_file and not os.path.exists(toolchain_file):
            raise FileNotFoundError(f'Toolchain file not found: {toolchain_file}')
        if toolchain_dir and not os.path.exists(toolchain_dir):
            raise FileNotFoundError(f'Toolchain directory not found: {toolchain_dir}')
        if not System.linux:
            raise RuntimeError('MIPS only supported on Linux')

        # check if we have already initialized the toolchain
        if self.gcc_prefix and self.mips_arch == arch \
            and self.toolchain_file == toolchain_file \
            and self.toolchain_dir == toolchain_dir:
            return

        # direct system installed MIPS toolchain
        self.mips_arch = arch
        self.toolchain_file = toolchain_file # additional toolchain to specify sysroot details

        # if a toolchain dir is provided, it should have a bin/ subdir with the compiler
        if toolchain_dir:
            self.toolchain_dir = toolchain_dir
            if os.path.exists(f'{toolchain_dir}/bin/{arch}-linux-gnu-gcc'):
                gcc_prefix = f'{toolchain_dir}/bin/{arch}-linux-gnu-'
                libs_path = f'{toolchain_dir}/lib'
                include_path = f'{toolchain_dir}/include'
                self._set_mips_toolchain_dir(gcc_prefix, libs_path, include_path)
                return # success

        # might also be at `/usr/mipsel-linux-gnu`

        # check for system installed one as fallback
        if os.path.exists(f'/usr/bin/{arch}-linux-gnu-gcc'):
            gcc_prefix = f'/usr/bin/{arch}-linux-gnu-'
            libs_path = f'/usr/{arch}-linux-gnu/lib'
            include_path = f'/usr/{arch}-linux-gnu/include'
            self._set_mips_toolchain_dir(gcc_prefix, libs_path, include_path)
            return # success

        raise EnvironmentError('No MIPS toolchain compilers detected, '+
                               f'try "sudo apt-get install g++-{arch}-linux-gnu"')

    def _set_mips_toolchain_dir(self, gcc_prefix, libs_path, include_path):
        self.gcc_prefix = gcc_prefix
        self.libs_path = libs_path if os.path.exists(libs_path) else ''
        self.include_path = include_path if os.path.exists(include_path) else ''

        # attempt to detect toolchain version from gcc linux/version.h
        try:
            version_file = path_join(self.include_path, 'linux/version.h')
            if os.path.exists(version_file):
                for line in read_lines_from(version_file):
                    if line.startswith('#define LINUX_VERSION_MAJOR'):
                        self.toolchain_major = int(line.split()[2])
                    elif line.startswith('#define LINUX_VERSION_PATCHLEVEL'):
                        self.toolchain_minor = int(line.split()[2])
        except:
            pass
        if self.config.print:
            console(f'Found MIPS tools: {self.gcc_prefix}gcc  linux-v{self.toolchain_major}.{self.toolchain_minor}')
            if self.libs_path:
                console(f'  MIPS syslibs: {self.libs_path}')


    def get_cxx_flags(self, add_flag: Callable[[str,str], None]):
        add_flag('-DMIPS', '1')
        if self.libs_path:
            add_flag(f'-L {self.libs_path}')
        for path in self.includes():
            add_flag(f'-I {path}')

    def get_cmake_build_opts(self) -> list:
        if self.toolchain_file:
            if self.config.print:
                console(f'MIPS Toolchain: {self.toolchain_file}')
            return [
                'MIPS=TRUE',
                f'CMAKE_TOOLCHAIN_FILE="{self.toolchain_file}"'
            ]
        opt = [
            'MIPS=TRUE',
            'CMAKE_SYSTEM_NAME=Linux',
            'CMAKE_SYSTEM_VERSION=1',
            f'CMAKE_SYSTEM_PROCESSOR={self.mips_arch}',
            'CMAKE_FIND_ROOT_PATH_MODE_PROGRAM=ONLY', # Search for compiler tools
            'CMAKE_FIND_ROOT_PATH_MODE_LIBRARY=ONLY', # Search for libraries and headers in the target directories only
            'CMAKE_FIND_ROOT_PATH_MODE_INCLUDE=ONLY',
        ]
        return opt
