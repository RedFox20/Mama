import os
from typing import Callable
from mama.utils.system import System, console


class Oclea:
    def __init__(self, config):
        ## Oclea CV25/CVXX
        self.config = config
        self.toolchain_file = None  ## for Docker based build, this is the aarch64_toolchain.cmake
        self.toolchain_dir = None
        self.compilers = ''  ## Oclea g++, gcc and ld
        self.sdk_path = ''  ## Path to Oclea SDK libs root
        self.sysroot_path = ''  ## Path to Oclea system libs root
        self.sdk_path  = ''  ## Root path to oclea sdk
        self.include_paths = []  ## Path to additional Oclea include dirs
        self.version = '' ## GCC Version


    def bin(self):
        """ {toolchain_path}/x86_64-ocleasdk-linux/usr/bin/aarch64-oclea-linux/ """
        if not self.compilers: self.init_default()
        return self.compilers


    def sdk(self):
        """ {toolchain_path}/x86_64-ocleasdk-linux/ """
        if not self.compilers: self.init_default()
        return self.sdk_path


    def sysroot(self):
        """ {toolchain_path}/aarch64-oclea-linux/ """
        if not self.compilers: self.init_default()
        return self.sysroot_path


    # forced includes that should be added to compiler flags as -I paths
    def includes(self):
        """ [ '{toolchain_path}/aarch64-oclea-linux/usr/include' ] """
        if not self.compilers: self.init_default()
        return self.include_paths


    def append_env_path(self, paths, env):
        path = os.getenv(env)
        if path: paths.append(path)


    def init_default(self):
        if not self.compilers:
            self.init_toolchain()


    def init_toolchain(self, toolchain_dir=None, toolchain_file=None):
        if not System.linux:
            raise RuntimeError('Oclea only supported on Linux')

        paths = []
        if toolchain_dir: paths += [ toolchain_dir ]
        paths += [ 'oclea-toolchain', 'oclea-toolchain/toolchain', '/opt/oclea/1.0' ]
        self.append_env_path(paths, 'OCLEA_HOME')
        self.append_env_path(paths, 'OCLEA_SDK')
        if System.linux: paths += ['/usr/bin/oclea', 
                                   '/usr/local/bin/oclea', 
                                   '/opt/oclea/1.0']
        
        compiler = 'usr/bin/aarch64-oclea-linux/aarch64-oclea-linux-gcc'
        for oclea_path in paths:
            # Check for legacy structure
            sdk_path_legacy = os.path.abspath(f'{oclea_path}/x86_64-ocleasdk-linux')
            sys_path_legacy = os.path.abspath(f'{oclea_path}/aarch64-oclea-linux')
            
            # Check for Yocto structure
            sdk_path_yocto = os.path.abspath(f'{oclea_path}/sysroots/x86_64-ocleasdk-linux')
            sys_path_yocto = os.path.abspath(f'{oclea_path}/sysroots/cortexa53-oclea-linux')
            
            if os.path.exists(f'{sdk_path_legacy}/{compiler}') and os.path.exists(sys_path_legacy):
                self.sdk_path = sdk_path_legacy
                self.sysroot_path = sys_path_legacy
                self.toolchain_file = toolchain_file
                self.toolchain_dir = toolchain_dir
            elif os.path.exists(f'{sdk_path_yocto}/{compiler}') and os.path.exists(sys_path_yocto):
                self.sdk_path = sdk_path_yocto
                self.sysroot_path = sys_path_yocto
                oclea_root = os.path.dirname(os.path.dirname(self.sdk_path))
                self.toolchain_file = f'{oclea_root}/aarch64_oclea_toolchain.cmake'
                self.toolchain_dir = oclea_root
            if self.sdk_path and self.sysroot_path:
                self.compilers = f'{self.sdk_path}/usr/bin/aarch64-oclea-linux/'
                self.include_paths = [ f'{self.sdk_path}/usr/include' ]
                cc = f'{self.compilers}aarch64-oclea-linux-gcc'
                self.version = self.config.get_gcc_clang_fullversion(cc, dumpfullversion=True)
                return # success
            
        if self.toolchain_file and not os.path.exists(self.toolchain_file):
            raise FileNotFoundError(f'Toolchain file not found: {self.toolchain_file}')
        if self.toolchain_dir and not os.path.exists(self.toolchain_dir):
            raise FileNotFoundError(f'Toolchain directory not found: {self.toolchain_dir}')
        
        if os.path.exists(self.compilers):
            if self.config.print:
                console(f'Found Oclea TOOLS: {self.compilers}')
                console(f'      Oclea SDK path: {self.sdk_path}')
                console(f'      Oclea sys path: {self.sysroot_path}')
        else:
            raise EnvironmentError(f'''No Oclea toolchain compilers detected! 
    Default search paths: {paths} 
    Define env OCLEA_HOME with path to Oclea tools.''')

    def get_cxx_flags(self, add_flag: Callable[[str,str], None]):
        add_flag('-march', 'armv8-a')
        add_flag('-mcpu', 'cortex-a53+crypto')
        add_flag('-mlittle-endian')
        add_flag('-DOCLEA', '1')
        # add_flag('--sysroot', self.sdk())
        # add_flag(f'-L{self.sysroot()}/lib')
        # add_flag(f'-L{self.sysroot()}/usr/lib')
        # add_flag(f'-L{self.sdk()}/lib')
        # add_flag(f'-L{self.sdk()}/usr/lib')
        # add_flag(f'-L{self.sdk()}/usr/libexec/aarch64-oclea-linux/gcc/aarch64-oclea-linux/9.3.0')
        for path in self.includes():
            add_flag(f'-I {path}')


    def get_cmake_build_opts(self) -> list:
        if self.toolchain_file:
            if self.config.print:
                console(f'Toolchain: {self.toolchain_file}')
            return [
                'OCLEA=TRUE',
                f'CMAKE_TOOLCHAIN_FILE="{self.toolchain_file}"'
            ]
        opt = [
            'OCLEA=TRUE',
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

        cc_prefix = f'{self.bin()}aarch64-oclea-linux-'
        environ['CC'] = cc_prefix + 'gcc'
        environ['CXX'] = cc_prefix + 'g++'
        environ['AR'] = cc_prefix + 'ar'
        environ['LD'] = cc_prefix + 'ld'
        environ['READELF'] = cc_prefix + 'readelf'
        environ['STRIP'] = cc_prefix + 'strip'
        environ['RANLIB'] = cc_prefix + 'ranlib'
        return environ
