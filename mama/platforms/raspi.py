from __future__ import annotations
from typing import TYPE_CHECKING, Callable
import os

from .platform import Platform
from .toolchain import Toolchain
from mama.utils.system import System, console
from mama.cmake_configure import cross_system_opts, use_toolchain_file

if TYPE_CHECKING:
    from ..build_target import BuildTarget


# Every Raspberry Pi since the Pi 3 is ARMv8, so arm64 is the default and arm is the legacy path. The
# triple drives the compiler name, the sysroot and the include dir, so a new arch is one entry here.
_TRIPLES = {'arm64': 'aarch64-linux-gnu', 'arm': 'arm-linux-gnueabihf'}
_MARCH = {'arm64': 'armv8-a', 'arm': 'armv7-a'}
# armv7-a on its own declares no FPU, but the gnueabihf triple defaults to -mfloat-abi=hard and
# then refuses to compile at all. neon-vfpv4 is what the Pi 2 and 3 (Cortex-A7/A53) actually have.
_FPU = {'arm': 'neon-vfpv4'}
SUPPORTED_ARCHES = tuple(_TRIPLES)


def triple_for_arch(arch: str) -> str:
    """The GNU triple mama cross-compiles with for a raspi arch, eg arm64 -> aarch64-linux-gnu."""
    if arch not in _TRIPLES:
        raise ValueError(f'Unsupported raspi arch={arch}! Supported={list(SUPPORTED_ARCHES)}')
    return _TRIPLES[arch]


class Raspi(Platform):
    """Raspberry Pi cross build. Owns its own toolchain discovery, the way Mips and GenericYocto do.
    BuildConfig stores which platform is active, never how to find its compilers."""
    name = 'raspi'
    arch_aliases = {'raspi32': 'arm'}  # legacy ARMv7
    is_cross = True
    is_host_runnable = False
    default_arch = 'arm64'  # every Pi since the 3 is ARMv8
    supported_arches = SUPPORTED_ARCHES
    platform_define = 'RASPI'
    build_dirs = {'arm64': 'raspi', 'arm': 'raspi32'}

    def __init__(self, config):
        super().__init__(config)
        self.toolchain_dir = ''   # root of the cross toolchain
        self.compilers = ''       # the bin/ dir holding <triple>-gcc
        self.sysroot = ''
        self.include_paths = []


    def triple(self) -> str:
        return triple_for_arch(self.arch())


    def compiler_prefix(self) -> str:
        """`<bin>/<triple>-`. Append `gcc` or `g++` for the full compiler path."""
        if not self.compilers: self.init_default()
        return f'{self.compilers}{self.triple()}-'


    def get_sysroot(self) -> str:
        if not self.compilers: self.init_default()
        return self.sysroot


    def get_includes(self) -> list:
        if not self.compilers: self.init_default()
        return self.include_paths


    def _search_paths(self) -> list:
        paths = []
        self.config.append_env_path(paths, 'RASPI_HOME')
        self.config.append_env_path(paths, 'RASPBERRY_HOME')
        if System.windows: paths += ['/SysGCC/raspberry']
        elif System.linux: paths += ['/usr/bin/raspberry', '/usr/local/bin/raspberry', '/opt/raspberry', '/usr']
        return paths


    def _layouts(self, root: str) -> list:
        """Where a toolchain root can keep its bin/ dir. The legacy Broadcom `tools` repo nests the 32-bit
        toolchain under arm-bcm2708/<triple>/, while a distro cross package (gcc-aarch64-linux-gnu) and the
        modern standalone toolchains put it straight in bin/."""
        return [root, f'{root}/arm-bcm2708/{self.triple()}']


    def init_default(self):
        """Find the cross toolchain for the CURRENT arch. Raises with the searched paths when none is
        installed: a silent fallback would build with the HOST gcc and quietly produce x86 binaries."""
        if self.compilers: return  # already resolved, or a mamafile set it explicitly
        triple = self.triple()
        ext = '.exe' if System.windows else ''
        searched = []
        for root in self._search_paths():
            for base in self._layouts(root):
                searched.append(base)
                if os.path.exists(f'{base}/bin/{triple}-gcc{ext}'):
                    self.init_toolchain(base)
                    return
        raise EnvironmentError(f'No Raspberry PI {self.arch()} toolchain detected! Looked for ' + \
                               f'<path>/bin/{triple}-gcc in: {searched}\n' + \
                               f'Install it (Debian: apt install gcc-{triple} g++-{triple})' + \
                               ' or set env RASPI_HOME to the toolchain root.')


    def init_toolchain(self, toolchain_dir: str = None, toolchain_file=None):
        """Point every path at `toolchain_dir`, whose bin/ must hold `<triple>-gcc`.

        A standalone toolchain carries its own `<triple>/sysroot`; a distro cross package
        (gcc-aarch64-linux-gnu) has none and its gcc already knows where the target headers live. So both
        the sysroot and the extra include dir are set ONLY if they exist - passing a --sysroot that is not
        there makes every compile fail on missing system headers."""
        triple = self.triple()
        self.toolchain_dir = toolchain_dir
        self.compilers = f'{toolchain_dir}/bin/'
        sysroot = f'{toolchain_dir}/{triple}/sysroot'
        includes = f'{toolchain_dir}/{triple}/lib/include'
        self.sysroot = sysroot if os.path.exists(sysroot) else ''
        self.include_paths = [includes] if os.path.exists(includes) else []
        if self.config.print:
            console(f'Found RASPI {self.arch()} TOOLS: {self.compilers}' + \
                    (f'\n    sysroot: {self.sysroot}' if self.sysroot else ' (compiler-provided sysroot)'))


    def _build_toolchain(self) -> Toolchain:
        prefix = self.compiler_prefix()
        ext = '.exe' if System.windows else ''
        return Toolchain(system_name=self.system_name, system_processor=self.system_processor(),
                         system_version='1', cc=f'{prefix}gcc{ext}', cxx=f'{prefix}g++{ext}',
                         tool_prefix=prefix, sysroot=self.get_sysroot(),
                         include_paths=tuple(self.get_includes()),
                         # NEVER, not ONLY: the toolchain has no binutils of its own on a distro cross
                         # package, so cmake must take the compiler tools mama named
                         find_root_program='NEVER')


    def get_cxx_flags(self, add_flag: Callable[[str, str], None]):
        arch = self.arch()
        add_flag('-march', _MARCH[arch])
        if arch in _FPU: add_flag('-mfpu', _FPU[arch])
        sysroot = self.get_sysroot()
        if sysroot: add_flag('--sysroot', sysroot)
        super().get_cxx_flags(add_flag)


    def get_cmake_build_opts(self, target: BuildTarget) -> list:
        config = self.config
        opts = ['RASPI=TRUE'] + cross_system_opts(config, self.system_name, self.system_processor()) + [
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
