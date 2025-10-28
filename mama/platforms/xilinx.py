from __future__ import annotations
from typing import TYPE_CHECKING
import os
from typing import Callable
from mama.utils.system import System, console, Color, get_colored_text


if TYPE_CHECKING:
    from ..build_config import BuildConfig


class Xilinx:
    def __init__(self, config: BuildConfig):
        ## Xilinx Zynq UltraScale+ MPSoC
        self.config = config
        self.toolchain_file = None  ## for custom builds, this is the aarch64_xilinx_toolchain.cmake
        self.toolchain_dir = None
        self.compilers = ''  ## Xilinx g++, gcc and ld
        self.sdk_path = ''  ## Path to Xilinx SDK libs root
        self.sysroot_path = ''  ## Path to Xilinx system libs root
        self.include_paths = []  ## Path to additional Xilinx include dirs
        self.version = '' ## GCC Version


    def bin(self):
        """ {toolchain_path}/sysroots/x86_64-petalinux-linux/usr/bin/aarch64-xilinx-linux/ """
        if not self.compilers: self.init_default()
        return self.compilers


    def sdk(self):
        """ {toolchain_path}/sysroots/x86_64-petalinux-linux/ """
        if not self.compilers: self.init_default()
        return self.sdk_path


    def sysroot(self):
        """ {toolchain_path}/sysroots/cortexa72-cortexa53-xilinx-linux/ """
        if not self.compilers: self.init_default()
        return self.sysroot_path


    # forced includes that should be added to compiler flags as -I paths
    def includes(self):
        """ [ '{toolchain_path}/sysroots/cortexa72-cortexa53-xilinx-linux/usr/include' ] """
        if not self.compilers: self.init_default()
        return self.include_paths


    def append_env_path(self, paths, env):
        path = os.getenv(env)
        if path: paths.append(path)


    def init_default(self):
        if not self.compilers:
            self.init_toolchain()


    def _set_toolchain_file(self, toolchain_file):
        if os.path.exists(toolchain_file):
            self.toolchain_file = toolchain_file
            return True
        else:
            console(f'No toolchain file found at: {toolchain_file}', color=Color.RED)
            return False


    def init_toolchain(self, toolchain_dir=None, toolchain_file=None):
        if not System.linux:
            raise RuntimeError('Xilinx only supported on Linux')

        paths = []
        if toolchain_dir: paths += [ toolchain_dir ]
        # this is the primary search path for Linux cross-builds:
        if System.linux: paths += [ '/opt/petalinux/toolchain' ]
        # these are generic ones:
        paths += [ 'xilinx-toolchain', 'xilinx-toolchain/toolchain' ]
        self.append_env_path(paths, 'XILINX_HOME')
        self.append_env_path(paths, 'XILINX_SDK')

        compiler = 'usr/bin/aarch64-xilinx-linux/aarch64-xilinx-linux-gcc'
        for xilinx_path in paths:
            # Check for Yocto/PetaLinux structure
            yocto_sdkpath = os.path.abspath(f'{xilinx_path}/sysroots/x86_64-petalinux-linux')
            yocto_sysroot = os.path.abspath(f'{xilinx_path}/sysroots/cortexa72-cortexa53-xilinx-linux')

            if os.path.exists(f'{yocto_sdkpath}/{compiler}') and os.path.exists(yocto_sysroot):
                self.sdk_path = yocto_sdkpath
                self.sysroot_path = yocto_sysroot
                self.toolchain_dir = os.path.abspath(xilinx_path)

                # if original `toolchain_dir` was chosen, then prefer toolchain_file
                if toolchain_file and xilinx_path == toolchain_dir:
                    self._set_toolchain_file(toolchain_file)
                else:
                    self._set_toolchain_file(f'{self.toolchain_dir}/aarch64_xilinx_toolchain.cmake')

            # if sdk_path and sysroot_path are configured, then we're done
            if self.sdk_path and self.sysroot_path:
                self.compilers = f'{self.sdk_path}/usr/bin/aarch64-xilinx-linux/'
                self.include_paths = [ f'{self.sysroot_path}/usr/include' ]
                cc = f'{self.compilers}aarch64-xilinx-linux-gcc'
                self.version = self.config.get_gcc_clang_fullversion(cc, dumpfullversion=True)
                break # success

        # fallback
        if not self.toolchain_file and toolchain_file:
            if not self._set_toolchain_file(toolchain_file):
                raise FileNotFoundError(f'Toolchain file not found: {toolchain_file}')

        if self.config.print:
            OK  = get_colored_text('OK', 'green')
            BAD = get_colored_text('NOTFOUND', 'red')
            tools     = OK if os.path.exists(self.compilers) else BAD
            sdk       = OK if os.path.exists(self.sdk_path) else BAD
            sysroot   = OK if os.path.exists(self.sysroot_path) else BAD
            toolchain = OK if self.toolchain_file and os.path.exists(self.toolchain_file) else BAD
            console(f'Found Xilinx TOOLS:     {tools} {self.compilers}')
            console(f'      Xilinx SDK path:  {sdk} {self.sdk_path}')
            console(f'      Xilinx sysroot:   {sysroot} {self.sysroot_path}')
            console(f'      Xilinx toolchain: {toolchain} {self.toolchain_file}')

        if not os.path.exists(self.compilers):
            raise EnvironmentError(f'''No Xilinx toolchain compilers detected!
    Default search paths: {paths}
    Define env XILINX_HOME with path to Xilinx tools.''')


    def get_cxx_flags(self, add_flag: Callable[[str,str], None]):
        # Match PetaLinux SDK environment-setup flags
        add_flag('-mcpu', 'cortex-a72.cortex-a53+crc')
        add_flag('-mbranch-protection', 'standard')
        add_flag('-DXILINX', '1')
        for path in self.includes():
            add_flag(f'-I {path}')


    def get_cmake_build_opts(self) -> list:
        if self.toolchain_file:
            if self.config.print:
                console(f'Toolchain: {self.toolchain_file}')
            return [
                'XILINX=TRUE',
                f'CMAKE_TOOLCHAIN_FILE="{self.toolchain_file}"'
            ]
        cc_prefix = f'{self.bin()}aarch64-xilinx-linux-'
        if self.config.print:
            console(f'Toolchain not specified, using Xilinx compilers from: {self.bin()}', color=Color.YELLOW)

        # NOTE: CMAKE_C_COMPILER and CMAKE_CXX_COMPILER is already configured
        #       by get_preferred_compiler_paths()
        opt = [
            'XILINX=TRUE',
            'PLATFORM=arm64',
            'CMAKE_SYSTEM_NAME=Linux',
            'CMAKE_SYSTEM_VERSION=1',
            'CMAKE_SYSTEM_PROCESSOR=aarch64',
            'CMAKE_SYSROOT='+self.sysroot(),
            'CMAKE_AR='+cc_prefix+'ar',
            'CMAKE_READELF='+cc_prefix+'readelf',
            'CMAKE_STRIP='+cc_prefix+'strip',
            'CMAKE_RANLIB='+cc_prefix+'ranlib',
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

        cc_prefix = f'{self.bin()}aarch64-xilinx-linux-'
        environ['CC'] = cc_prefix + 'gcc'
        environ['CXX'] = cc_prefix + 'g++'
        environ['AR'] = cc_prefix + 'ar'
        environ['LD'] = cc_prefix + 'ld'
        environ['READELF'] = cc_prefix + 'readelf'
        environ['STRIP'] = cc_prefix + 'strip'
        environ['RANLIB'] = cc_prefix + 'ranlib'
        return environ
