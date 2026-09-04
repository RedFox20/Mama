from __future__ import annotations
from typing import Callable
import os

from .platform import Platform
from .toolchain import Toolchain
from mama.utils.system import System, console


class GnuCross(Platform):
    """A Linux target built with a plain GNU cross toolchain: `<triple>-gcc` in some `bin/` dir and
    nothing else around it. A distro cross package (`apt install g++-aarch64-linux-gnu`) and a
    standalone toolchain tarball are both this shape, so a board is data: its triples, its marches
    and where to look for them.

    A Yocto SDK is NOT this shape. It ships a sysroot and its own cmake toolchain file, and
    `GenericYocto` describes that layout instead.
    """
    system_name = 'Linux'
    is_cross = True
    is_host_runnable = False

    ## --- everything a board declares ---
    display_name = ''   ## what a message calls this toolchain, eg 'Raspberry PI'. '' falls back to `name`
    triples = {}        ## arch to GNU triple, eg {'arm64': 'aarch64-linux-gnu'}
    marches = {}        ## arch to -march. An arch that is absent names none
    mfpus = {}          ## arch to -mfpu, only where the ABI leaves the FPU unnamed
    search_envs = ()    ## env vars naming a toolchain root, read first and in that order
    linux_paths = ()    ## default roots on a Linux host, most specific first
    windows_paths = ()  ## default roots on a Windows host


    def __init_subclass__(cls, **kwargs):
        """Derive what the triples already say, so a board declares its arches once."""
        super().__init_subclass__(**kwargs)
        if cls.triples and not cls.__dict__.get('supported_arches'):
            cls.supported_arches = tuple(cls.triples)


    def __init__(self, config):
        super().__init__(config)
        self.toolchain_dir = ''  ## root of the cross toolchain
        self.compilers = ''      ## the bin/ dir holding <triple>-gcc
        self.sysroot = ''        ## only set when the toolchain ships one
        self.include_paths = []


    def _name(self) -> str:
        """What a message calls this toolchain. Read, never assigned: a subclass of a subclass must
        inherit the base's display_name instead of silently falling back to its own bare name."""
        return self.display_name or self.name


    @classmethod
    def triple_for(cls, arch: str) -> str:
        """The GNU triple this platform cross-compiles an arch with, eg arm64 -> aarch64-linux-gnu."""
        if arch not in cls.triples:
            raise ValueError(f'Unsupported {cls.name} arch={arch}! Supported={list(cls.triples)}')
        return cls.triples[arch]


    def triple(self) -> str:
        return self.triple_for(self.arch())


    def compiler_prefix(self) -> str:
        """`<bin>/<triple>-`. Append `gcc` or `g++` for the full compiler path."""
        if not self.compilers: self.init_default()
        return f'{self.compilers}{self.triple()}-'


    def archiver(self) -> str:
        """The cross binutils sit beside the compiler, and the host keeps them off PATH. The name is a
        full path, which Windows never completes from PATHEXT, so it carries its own suffix."""
        return f'{self.compiler_prefix()}ar' + ('.exe' if System.windows else '')


    def get_sysroot(self) -> str:
        if not self.compilers: self.init_default()
        return self.sysroot


    def get_includes(self) -> list:
        if not self.compilers: self.init_default()
        return self.include_paths


    def _search_paths(self) -> list:
        """Every root discovery looks in, env vars first so a user override always wins."""
        paths = []
        for env in self.search_envs:
            self.config.append_env_path(paths, env)
        if System.windows: paths += list(self.windows_paths)
        elif System.linux: paths += list(self.linux_paths)
        return paths


    def _layouts(self, root: str) -> list:
        """Where a toolchain root can keep its bin/ dir. Straight in bin/ for everything but the odd
        vendor tree, which overrides this to name its nesting."""
        return [root]


    def _install_hint(self, triple: str) -> str:
        env = f' or set env {self.search_envs[0]} to the toolchain root.' if self.search_envs else '.'
        return f'Install it (Debian: apt install gcc-{triple} g++-{triple})' + env


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
        raise EnvironmentError(f'No {self._name()} {self.arch()} toolchain detected! Looked for ' + \
                               f'<path>/bin/{triple}-gcc in: {searched}\n' + self._install_hint(triple))


    def init_toolchain(self, toolchain_dir: str = None, toolchain_file=None):
        """Point every path at the toolchain root. Set the sysroot and the extra include dir ONLY
        when they exist: a distro cross package has none, its gcc already knows the target headers,
        and a --sysroot that is not there makes every compile fail on missing system headers.
        toolchain_dir: the toolchain root, its bin/ must hold `<triple>-gcc`
        toolchain_file: unused, a GNU cross toolchain ships no build-system file of its own
        """
        triple = self.triple()
        self.toolchain_dir = toolchain_dir
        self.compilers = f'{toolchain_dir}/bin/'
        sysroot = f'{toolchain_dir}/{triple}/sysroot'
        includes = f'{toolchain_dir}/{triple}/lib/include'
        self.sysroot = sysroot if os.path.exists(sysroot) else ''
        self.include_paths = [includes] if os.path.exists(includes) else []
        if self.config.print:
            console(f'Found {self._name()} {self.arch()} TOOLS: {self.compilers}' + \
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
        return self.marches.get(self.arch(), '')


    def get_cxx_flags(self, add_flag: Callable[[str, str], None]):
        arch = self.arch()
        if arch in self.mfpus: add_flag('-mfpu', self.mfpus[arch])
        sysroot = self.get_sysroot()
        if sysroot: add_flag('--sysroot', sysroot)
        super().get_cxx_flags(add_flag)
