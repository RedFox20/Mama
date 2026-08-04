from __future__ import annotations
from typing import Callable
import os, re

from .platform import Platform
from .toolchain import Toolchain
from mama.util import path_join
from mama.utils.system import System, console, warning, Color, get_colored_text


class GenericYocto(Platform):
    """A Yocto SDK based embedded Linux board. Every such SDK lays its files out the same way, so a
    board only declares its search paths, its compiler triple and its sysroot name."""
    system_name = 'Linux'
    is_cross = True
    cxx20_flag = 'c++2a'  # these SDKs ship gcc older than the final C++20 name
    is_host_runnable = False
    default_arch = 'arm64'
    supported_arches = ('arm64',)
    ## Everything a board declares. The SDK layout is identical across vendors, so a board is data.
    host_triple  = 'aarch64-poky-linux'  ## the GNU --host triple
    search_paths = ()                    ## where to look for the SDK, most specific first
    search_envs  = ()                    ## env vars naming the SDK root. Defaults to <NAME>_SDK_HOME
    compiler_name = 'usr/bin/aarch64-poky-linux/aarch64-poky-linux-gcc'  ## relative to the host sysroot
    sdk_name      = 'x86_64-pokysdk-linux'          ## sysroots/<sdk_name>/ holds the cross compilers
    sysroot_name  = 'cortexa53-crypto-poky-linux'   ## sysroots/<sysroot_name>/ holds the target libs
    default_toolchain = 'usr/share/cmake/cortexa53-crypto-poky-linux-toolchain.cmake'  ## in the SDK root

    def __init__(self, config):
        super().__init__(config)
        self.toolchain_file = ''  ## for a Docker based build, this is the aarch64_toolchain.cmake
        self.toolchain_dir = ''
        self.compilers = ''  ## dir holding g++, gcc and ld
        self.cc_prefix = '' ## e.g. '{self.compilers}aarch64-poky-linux-'
        self.sdk_path = ''  ## Path to SDK libs root
        self.sysroot_path = ''  ## Path to system libs root
        self.include_paths = []  ## Path to additional include dirs
        self.version = '' ## GCC Version
        self.sdk_version = (1,0,0) ## SDK version tuple, e.g. (5, 0, 4) for version 5.0.4


    def __init_subclass__(cls, **kwargs):
        """Derive the board's defines from its name, so a board declares the name once. YOCTO_LINUX
        goes out for every generic Yocto board, on top of the board's own define."""
        super().__init_subclass__(**kwargs)
        if cls.name and not cls.platform_define:
            cls.platform_define = cls.name.upper()
            cls.compile_defines = {cls.platform_define: '1', 'YOCTO_LINUX': '1'}


    def _resolved(self):
        """Run SDK discovery if it has not run yet, then return self. Reading a path field before
        that silently hands back ''."""
        if not self.compilers: self.init_default()
        return self


    def sysroot(self):
        """ {sdk_path}/sysroots/cortexa53-crypto-poky-linux/ """
        return self._resolved().sysroot_path


    def gcc_prefix(self):
        """ e.g. {sdk_path}/usr/bin/aarch64-poky-linux/aarch64-poky-linux- """
        return self._resolved().cc_prefix


    def gnu_host_triple(self) -> str:
        return self.host_triple


    def distro_version(self) -> tuple:
        """The SDK version, eg (5, 0, 4). Unlike every other platform this carries no name, and the
        artifactory archive name has always been built from it that way."""
        return self._resolved().sdk_version


    def append_env_path(self, paths, env):
        path = os.getenv(env)
        if path: paths.append(path)


    def init_default(self):
        if not self.compilers:
            self.init_toolchain()


    @staticmethod
    def expand_versioned_sdks(paths: list) -> list:
        """Expands each path with its versioned SDK installs, e.g. /opt/imx8mp-sdk/1.4.0,
        newest first, so a versioned install wins over a flat legacy layout at the same root."""
        expanded = []
        for path in paths:
            if os.path.isdir(path):
                versions = []
                for name in os.listdir(path):
                    if name and all(p.isdigit() for p in name.split('.')):
                        versions.append(name)
                # listdir order is not guaranteed cross-platform, sort newest first
                versions.sort(key=lambda n: [int(p) for p in n.split('.')], reverse=True)
                expanded += [path_join(path, v) for v in versions]
            expanded.append(path)
        return expanded


    def init_toolchain(self, toolchain_dir=None, toolchain_file=None):
        """Find the SDK. An explicit `toolchain_dir` is searched first, then the board's own paths,
        then whatever its env vars name."""
        # TODO: expand support to enable Windows host cross-compilation?
        if not System.linux:
            raise RuntimeError(f'{self.name} only supported on Linux')

        paths = ([toolchain_dir] if toolchain_dir else []) + list(self.search_paths)
        # fallback env for user configuration, e.g. XILINX_SDK_HOME
        envs = list(self.search_envs) or [f'{self.platform_define}_SDK_HOME']
        for env in envs:
            self.append_env_path(paths, env)
        paths = GenericYocto.expand_versioned_sdks(paths)

        for path in paths:
            # Check for Yocto structure
            yocto_sdkpath = os.path.abspath(f'{path}/sysroots/{self.sdk_name}')
            yocto_sysroot = os.path.abspath(f'{path}/sysroots/{self.sysroot_name}')
            yocto_compiler = f'{yocto_sdkpath}/{self.compiler_name}'

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
                    self._set_toolchain_file(f'{self.toolchain_dir}/{self.default_toolchain}')

                self.compilers = os.path.dirname(yocto_compiler) + '/' # e.g. f'{self.sdk_path}/usr/bin/aarch64-poky-linux/'
                # replace -gcc at the end with '-' to get the prefix
                # e.g '{self.sdk_path}/usr/bin/aarch64-poky-linux/aarch64-poky-linux-
                self.cc_prefix = self.compilers + os.path.basename(self.compiler_name).replace('-gcc', '-')
                self.include_paths = [ f'{self.sysroot_path}/usr/include' ]
                self.version = self.config.get_gcc_clang_fullversion(yocto_compiler, dumpfullversion=True)
                break

            # add some helpful debug messages on potentially broken toolchain configurations
            if self.config.print and found_compiler != found_sysroot:
                if found_compiler: warning(f'Found compiler at {yocto_compiler} but sysroot not found at {yocto_sysroot}')
                else:              warning(f'Found sysroot at {yocto_sysroot} but compiler not found at {yocto_compiler}')

        # fallback
        if not self.toolchain_file and toolchain_file:
            if not self._set_toolchain_file(toolchain_file):
                raise FileNotFoundError(f'Toolchain file not found: {toolchain_file}')

        self.sdk_version = self._autodetect_version(self.toolchain_dir)
        if self.config.print: self._print_toolchain_status()

        if not os.path.exists(self.compilers):
            raise EnvironmentError(f'''No {self.name} toolchain compilers detected!
    Default search paths: {paths}
    Define env {envs[0]} with path to {self.name} tools.''')


    def _print_toolchain_status(self):
        OK  = get_colored_text('OK', 'green')
        BAD = get_colored_text('NOTFOUND', 'red')
        def status(path):
            return OK if path and os.path.exists(path) else BAD
        tools = 'TOOLS ' + '.'.join(str(x) for x in self.sdk_version) + ':'
        console(f'Yocto {self.name} {tools:14} {status(self.compilers)} {self.compilers}')
        console(f'      {self.name} SDK path:      {status(self.sdk_path)} {self.sdk_path}')
        console(f'      {self.name} sysroot:       {status(self.sysroot_path)} {self.sysroot_path}')
        console(f'      {self.name} toolchain:     {status(self.toolchain_file)} {self.toolchain_file}')


    def _autodetect_version(self, toolchain_dir):
        """Autodetect the version of the Yocto SDK from the toolchain_dir name."""
        if not toolchain_dir:
            return (1, 0, 0)
        last_part = os.path.basename(toolchain_dir)
        if last_part.count('.') == 2: # e.g. 'toolchain-1.0.0' or '5.0.4'
            parts = re.split(r'[-_ +]', last_part) # split using separators
            # find last part that starts with a digit and looks like a version number, e.g. '1.0' or '5.0.4'
            version = [p for p in parts if p and '.' in p and p[0].isdigit()][-1]
            return tuple(int(x) for x in version.split('.') if x.isdigit())
        return (1, 0, 0)


    def _set_toolchain_file(self, toolchain_file):
        if os.path.exists(toolchain_file):
            self.toolchain_file = toolchain_file
            return True
        console(f'No toolchain file found at: {toolchain_file}', color=Color.RED)
        return False


    def _build_toolchain(self) -> Toolchain:
        prefix = self.cc_prefix  # discovery already ran: Platform.toolchain() calls init_default() first
        # find_root_program NEVER, so the build system takes the cross binutils named here, and the
        # target's own libs and headers instead of the host's
        return Toolchain(system_name=self.system_name, system_processor=self.system_processor(),
                         system_version='1', cc=f'{prefix}gcc', cxx=f'{prefix}g++', version=self.version,
                         tool_prefix=prefix, sysroot=self.sysroot_path, find_root_program='NEVER',
                         include_paths=tuple(self.include_paths), install_rpath=True,
                         toolchain_file=self.toolchain_file, toolchain_file_is_complete=True)


    def get_ld_flags(self, add_ld_flag: Callable[[str, str], None]):
        # -Wl,--as-needed keeps an embedded binary from linking libraries it never calls, which
        # bloats it and can break at runtime on a resource-constrained device
        add_ld_flag('-Wl,--as-needed')


    def get_gnu_build_env(self, environ: dict = {}):
        sysroot = f'--sysroot={self.sysroot()}'  # accessor: it resolves the SDK if nothing has yet
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
