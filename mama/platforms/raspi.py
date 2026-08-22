from __future__ import annotations
from typing import Callable
import os

from .platform import Platform
from .toolchain import Toolchain
from mama.utils.system import System, console


# The triple drives the compiler name, the sysroot and the include dir, so a new arch is one entry here.
_TRIPLES = {'arm64': 'aarch64-linux-gnu', 'arm': 'arm-linux-gnueabihf'}
_MARCH = {'arm64': 'armv8-a', 'arm': 'armv7-a'}
# The gnueabihf triple defaults to hard float, so armv7-a must name an FPU. neon-vfpv4 is what the Pi 2 and 3 have.
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
    cxx20_flag = 'c++2a'  # these SDKs ship gcc older than the final C++20 name
    is_host_runnable = False
    default_arch = 'arm64'  # every Pi since the 3 is ARMv8
    supported_arches = SUPPORTED_ARCHES
    platform_define = 'RASPI'
    toolchain_override_attr = 'cmake_raspi_toolchain'
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


    def archiver(self) -> str:
        """The cross binutils sit beside the compiler, and the host keeps them off PATH."""
        return f'{self.compiler_prefix()}ar'


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
        """Where a toolchain root can keep its bin/ dir. The legacy Broadcom `tools` repo nests the
        32-bit toolchain under arm-bcm2708/<triple>/. Every other toolchain puts it straight in bin/."""
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
        """Point every path at the toolchain root. Set the sysroot and the extra include dir ONLY
        when they exist: a distro cross package has none, its gcc already knows the target headers,
        and a --sysroot that is not there makes every compile fail on missing system headers.
        toolchain_dir: the toolchain root, its bin/ must hold `<triple>-gcc`
        """
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
                         include_paths=tuple(self.get_includes()),
                         # NEVER, not ONLY: a distro cross package ships no binutils and no sysroot, so
                         # the build system takes the tools mama named, and the sysroot goes as a flag
                         find_root_program='NEVER')


    def default_march(self) -> str:
        return _MARCH[self.arch()]


    def get_cxx_flags(self, add_flag: Callable[[str, str], None]):
        arch = self.arch()
        if arch in _FPU: add_flag('-mfpu', _FPU[arch])
        sysroot = self.get_sysroot()
        if sysroot: add_flag('--sysroot', sysroot)
        super().get_cxx_flags(add_flag)
