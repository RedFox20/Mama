from __future__ import annotations
from typing import TYPE_CHECKING
import os
from typing import Callable
from mama.utils.system import System, console, Color, get_colored_text


if TYPE_CHECKING:
    from ..build_config import BuildConfig


class GenericYocto:
    def __init__(self, name: str, platform_define, config: BuildConfig):
        self.name = name ## e.g. 'imx8mp' or 'oclea' or 'xilinx'
        self.platform_define = platform_define ## e.g. 'IMX8MP' or 'OCLEA' or 'XILINX'
        self.config = config
        self.toolchain_file = ''  ## for Docker based build, this is the aarch64_toolchain.cmake
        self.toolchain_dir = ''
        self.compilers = ''  ## g++, gcc and ld
        self.cc_prefix = '' ## e.g. '{self.compilers}aarch64-poky-linux-'
        self.sdk_path = ''  ## Path to SDK libs root
        self.sysroot_path = ''  ## Path to system libs root
        self.include_paths = []  ## Path to additional include dirs
        self.version = '' ## GCC Version


    def bin(self):
        """ {sdk_path}/sysroots/x86_64-pokysdk-linux/usr/bin/aarch64-poky-linux/ """
        if not self.compilers: self.init_default()
        return self.compilers


    def sdk(self):
        """ {sdk_path}/sysroots/x86_64-pokysdk-linux/ """
        if not self.compilers: self.init_default()
        return self.sdk_path


    def sysroot(self):
        """ {sdk_path}/sysroots/cortexa53-crypto-poky-linux/ """
        if not self.compilers: self.init_default()
        return self.sysroot_path


    # forced includes that should be added to compiler flags as -I paths
    def includes(self):
        """ [ '{sdk_path}/sysroots/cortexa53-crypto-poky-linux/usr/include' ] """
        if not self.compilers: self.init_default()
        return self.include_paths

    def gcc_prefix(self):
        """ e.g. {sdk_path}/usr/bin/aarch64-poky-linux/aarch64-poky-linux- """
        if not self.compilers: self.init_default()
        return self.cc_prefix


    def append_env_path(self, paths, env):
        path = os.getenv(env)
        if path: paths.append(path)


    def init_default(self):
        if not self.compilers:
            self.init_toolchain()


    def init_toolchain(self, toolchain_dir=None, toolchain_file=None):
        raise NotImplementedError('init_toolchain must be implemented by subclass')


    def _yocto_toolchain_init(self, toolchain_dir=None, toolchain_file=None,
                              paths=[], envs=[],
                              compiler_name='usr/bin/aarch64-poky-linux/aarch64-poky-linux-gcc',
                              sdk_name='x86_64-pokysdk-linux',
                              sysroot_name='cortexa53-crypto-poky-linux',
                              default_toolchain='usr/share/cmake/cortexa53-crypto-poky-linux-toolchain.cmake'):
        # TODO: expand support to enable Windows host cross-compilation?
        if not System.linux:
            raise RuntimeError(f'{self.name} only supported on Linux')

        # add fallback define for user configuration e.g. XILINX_SDK_HOME
        if not envs:
            envs = [ f'{self.platform_define}_SDK_HOME' ]
        for env in envs:
            self.append_env_path(paths, env)

        for path in paths:
            # Check for Yocto structure
            yocto_sdkpath = os.path.abspath(f'{path}/sysroots/{sdk_name}')
            yocto_sysroot = os.path.abspath(f'{path}/sysroots/{sysroot_name}')
            yocto_compiler = f'{yocto_sdkpath}/{compiler_name}'

            if self.config.verbose:
                console(f'Checking for {self.name} toolchain in: {yocto_compiler} and {yocto_sysroot}')

            found_compiler = os.path.exists(yocto_compiler)
            found_sysroot = os.path.exists(yocto_sysroot)
            if found_compiler and found_sysroot:
                self.sdk_path     = yocto_sdkpath # e.g. {path}/sysroots/x86_64-pokysdk-linux
                self.sysroot_path = yocto_sysroot # e.g. {path}/sysroots/cortexa53-crypto-poky-linux
                self.toolchain_dir = os.path.abspath(path)

                # if original `toolchain_dir` was chosen, then prefer toolchain_file
                if toolchain_file and path == toolchain_dir:
                    self._set_toolchain_file(toolchain_file)
                else:
                    self._set_toolchain_file(f'{self.toolchain_dir}/{default_toolchain}')

                if self.sdk_path and self.sysroot_path:
                    self.compilers = os.path.dirname(yocto_compiler) + '/' # e.g. f'{self.sdk_path}/usr/bin/aarch64-poky-linux/'
                    # replace -gcc at the end with '-' to get the prefix
                    # e.g '{self.sdk_path}/usr/bin/aarch64-poky-linux/aarch64-poky-linux-
                    self.cc_prefix = self.compilers + os.path.basename(compiler_name).replace('-gcc', '-')
                    self.include_paths = [ f'{self.sysroot_path}/usr/include' ]
                    self.version = self.config.get_gcc_clang_fullversion(yocto_compiler, dumpfullversion=True)
                    break

            # add some helpful debug messages on potentially broken toolchain configurations
            if found_compiler and not found_sysroot:
                if self.config.print:
                    console(f'Found compiler at {yocto_compiler} but sysroot not found at {yocto_sysroot}', color=Color.YELLOW)
            elif not found_compiler and found_sysroot:
                if self.config.print:
                    console(f'Found sysroot at {yocto_sysroot} but compiler not found at {yocto_compiler}', color=Color.YELLOW)

        # fallback
        if not self.toolchain_file and toolchain_file:
            if not self._set_toolchain_file(toolchain_file):
                raise FileNotFoundError(f'Toolchain file not found: {toolchain_file}')

        if self.config.print:
            OK  = get_colored_text('OK', 'green')
            BAD = get_colored_text('NOTFOUND', 'red')
            def get_path_status(path):
                return OK if path and os.path.exists(path) else BAD

            console(f'Yocto {self.name} TOOLS:     {get_path_status(self.compilers)} {self.compilers}')
            console(f'      {self.name} SDK path:  {get_path_status(self.sdk_path)} {self.sdk_path}')
            console(f'      {self.name} sysroot:   {get_path_status(self.sysroot_path)} {self.sysroot_path}')
            console(f'      {self.name} toolchain: {get_path_status(self.toolchain_file)} {self.toolchain_file}')

        if not os.path.exists(self.compilers):
            raise EnvironmentError(f'''No {self.name} toolchain compilers detected! 
    Default search paths: {paths} 
    Define env {envs[0]} with path to {self.name} tools.''')


    def _set_toolchain_file(self, toolchain_file):
        if os.path.exists(toolchain_file):
            self.toolchain_file = toolchain_file
            return True
        else:
            console(f'No toolchain file found at: {toolchain_file}', color=Color.RED)
            return False


    def _add_common_cxx_flags(self, add_flag: Callable[[str,str], None]):
        # e.g -DIMX8MP=1 or -DOCLEA=1 or -DXILINX=1
        add_flag(f'-D{self.platform_define}', '1')
        # define YOCTO_LINUX=1 for all generic yocto embedded platforms
        add_flag(f'-DYOCTO_LINUX', '1')
        for path in self.includes():
            add_flag(f'-I {path}')


    def get_cmake_build_opts(self) -> list:
        if self.toolchain_file:
            if self.config.print:
                console(f'Toolchain: {self.toolchain_file}')
            return [
                f'{self.platform_define}=TRUE',
                f'CMAKE_TOOLCHAIN_FILE="{self.toolchain_file}"'
            ]
        # NOTE: CMAKE_C_COMPILER and CMAKE_CXX_COMPILER is already configured
        #       by get_preferred_compiler_paths()
        opt = [
            f'{self.platform_define}=TRUE',
            'CMAKE_SYSTEM_NAME=Linux',
            'CMAKE_SYSTEM_VERSION=1',
            'CMAKE_SYSTEM_PROCESSOR=arm64',
            'CMAKE_SYSROOT='+self.sysroot(),
            'CMAKE_AR='+self.cc_prefix+'ar',
            'CMAKE_READELF='+self.cc_prefix+'readelf',
            'CMAKE_STRIP='+self.cc_prefix+'strip',
            'CMAKE_RANLIB='+self.cc_prefix+'ranlib',
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

        environ['CC'] = self.cc_prefix + 'gcc'
        environ['CXX'] = self.cc_prefix + 'g++'
        environ['AR'] = self.cc_prefix + 'ar'
        environ['LD'] = self.cc_prefix + 'ld'
        environ['READELF'] = self.cc_prefix + 'readelf'
        environ['STRIP'] = self.cc_prefix + 'strip'
        environ['RANLIB'] = self.cc_prefix + 'ranlib'
        return environ

