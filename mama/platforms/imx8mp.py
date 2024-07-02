import os
from typing import Callable
from mama.utils.system import System, console


class Imx8mp:
    def __init__(self, config):
        self.config = config
        self.toolchain_file = None  ## for Docker based build, this is the aarch64_toolchain.cmake
        self.compilers = ''  ## IMX8MP g++, gcc and ld
        self.sdk_path = ''  ## Path to IMX8MP SDK libs root
        self.sysroot_path = ''  ## Path to IMX8MP system libs root
        self.sdk_path  = ''  ## Root path to IMX8MP sdk
        self.include_paths = []  ## Path to additional IMX8MP include dirs
        self.version = '' ## GCC Version


    def bin(self):
        if not self.compilers: self.init_default()
        return self.compilers


    def sdk(self):
        if not self.compilers: self.init_default()
        return self.sdk_path


    def sysroot(self):
        if not self.compilers: self.init_default()
        return self.sysroot_path


    # forced includes that should be added to compiler flags as -I paths
    def includes(self):
        if not self.compilers: self.init_default()
        return self.include_paths


    def append_env_path(self, paths, env):
        path = os.getenv(env)
        if path: paths.append(path)


    def init_default(self):
        if not self.compilers:
            self.init_toolchain()


    def init_toolchain(self, toolchain_dir=None, toolchain_file=None):
        print("INIT TOOLCHAIN",toolchain_dir,toolchain_file)

        self.sdk_path = '/opt/imx8mp-sdk/'
        self.sysroot_path = '/opt/imx8mp-sdk/sysroots/armv8a-poky-linux/'
        self.compilers = f'{self.sdk_path}sysroots/x86_64-pokysdk-linux/usr/bin/aarch64-poky-linux/'
        self.include_paths = f'{self.sdk_path}sysroots/x86_64-pokysdk-linux/usr/include/'

    def get_cxx_flags(self, add_flag: Callable[[str,str], None]):
        add_flag('-march', 'armv8-a')
        add_flag('-mcpu', 'cortex-a53+crypto')
        add_flag('-mlittle-endian')
        add_flag('-DIMX8MP', '1')

        for path in self.includes():
            add_flag(f'-I {path}')


    def get_cmake_build_opts(self) -> list:
        if self.toolchain_file:
            if self.config.print:
                console(f'Toolchain: {self.toolchain_file}')
            return [
                'IMX8MP=TRUE',
                f'CMAKE_TOOLCHAIN_FILE="{self.toolchain_file}"'
            ]
        opt = [
            'IMX8MP=TRUE',
            'CMAKE_SYSTEM_NAME=Linux',
            'CMAKE_SYSTEM_VERSION=1',
            'CMAKE_SYSTEM_PROCESSOR=arm64',
            'CMAKE_SYSROOT='+self.sysroot(),
            'CMAKE_FIND_ROOT_PATH_MODE_PROGRAM=NEVER', # Use our definitions for compiler tools
            'CMAKE_FIND_ROOT_PATH_MODE_LIBRARY=ONLY', # Search for libraries and headers in the target directories only
            'CMAKE_FIND_ROOT_PATH_MODE_INCLUDE=ONLY',
        ]
        return opt


    def get_gnu_build_env(self, environ: dict = {}):
        sysroot = f'--sysroot={self.sysroot()}'
        environ['LDFLAGS'] = sysroot
        environ['CFLAGS'] = sysroot
        environ['CXXFLAGS'] = sysroot

        cc_prefix = f'{self.bin()}aarch64-poky-linux-'
        environ['CC'] = cc_prefix + 'gcc'
        environ['CXX'] = cc_prefix + 'g++'
        environ['AR'] = cc_prefix + 'ar'
        environ['LD'] = cc_prefix + 'ld'
        environ['READELF'] = cc_prefix + 'readelf'
        environ['STRIP'] = cc_prefix + 'strip'
        environ['RANLIB'] = cc_prefix + 'ranlib'
        return environ
