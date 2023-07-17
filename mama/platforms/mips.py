import os
from typing import Callable
from mama.utils.system import System, console

class Mips:
    def __init__(self, config):
        self.config = config
        self.toolchain_file = None
        self.supported_arches = ['mips', 'mipsel', 'mips64', 'mips64el']
        self.mips_arch = 'mipsel' # prefer little endian mips
        self.gcc_prefix = ''
        self.libs_path = ''
        self.include_paths = []


    # returns the current path prefix where the compiler can be found
    # example: "/opt/mipsel-openwrt-linux/bin/mipsel-openwrt-linux-"
    def compiler_prefix(self):
        if not self.gcc_prefix: self.init_default()
        return self.gcc_prefix


    def includes(self):
        return self.include_paths


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

        # direct system installed MIPS toolchain
        self.mips_arch = arch
        self.toolchain_file = toolchain_file # additional toolchain to specify sysroot details

        arch_compiler = f'/usr/bin/{arch}-linux-gnu-gcc'
        if os.path.exists(arch_compiler):
            self.gcc_prefix = f'/usr/bin/{arch}-linux-gnu-'
            libs_path = f'/usr/{arch}-linux-gnu/lib'
            if os.path.exists(libs_path):
                self.libs_path = libs_path
            if self.config.print:
                console(f'Found MIPS tools: {self.gcc_prefix}gcc')
                if libs_path:
                    console(f'  MIPS syslibs: {libs_path}')
            return # success

        raise EnvironmentError('No MIPS toolchain compilers detected, '+
                               f'try "sudo apt-get install g++-{arch}-linux-gnu"')


    def _init_root_path(self, root_path):
        
        pass


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
