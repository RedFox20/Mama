from __future__ import annotations
from typing import TYPE_CHECKING, Callable
from .toolchain import Toolchain
from ..utils.system import System

if TYPE_CHECKING:
    from ..build_config import BuildConfig


# The canonical processor name per mama arch. This names what the TARGET runs on, never the host.
# A project that branches on it (googletest adds -march=x86-64-v3 the moment it reads x86_64)
# compiles host instructions into a cross build if the host value leaks in.
SYSTEM_PROCESSORS = {
    'arm64': 'aarch64', 'arm': 'armv7-a', 'x64': 'x86_64', 'x86': 'i686',
    'mips': 'mips', 'mipsel': 'mipsel', 'mips64': 'mips64', 'mips64el': 'mips64el',
}


def host_arch() -> str:
    """The arch of the machine mama runs on. The default target arch for a native build."""
    if System.aarch64: return 'arm64'
    if System.x86_64:  return 'x64'
    return 'x86'


def native_march(arch: str) -> str:
    """-march for a NATIVE build: 'native' when the host IS the target, else the baseline for the
    arch. Only a native platform may use this - 'native' on a cross build compiles host instructions."""
    if arch == 'arm64': return 'native' if System.aarch64 else 'armv8-a'
    if arch == 'x64':   return 'native' if System.x86_64 else 'x86-64'
    if arch == 'x86':   return 'native' if System.x86 else 'pentium4'
    raise RuntimeError(f'Unsupported arch: {arch}')


class Platform:
    """One target platform: what to build FOR, and how to find the tools that build it.

    Every platform mama supports is a subclass. Exactly one instance lives at `config.platform`.
    A subclass overrides only what differs from the defaults below. `Linux` overrides three
    members. `Android` overrides nine.

    A platform describes facts. It never formats a build-system option. `mama/buildsys/` renders
    `toolchain()` into whatever the active build system needs, so a second build system costs no
    change here.
    """

    ## --- identity, declared by the subclass ---
    name = ''                  ## 'linux', 'android', 'imx8mp'. Also the CLI arg and the archive tag
    cli_aliases = ()           ## extra CLI args that select this platform, eg ('windows', 'msvc')
    arch_aliases = {}          ## CLI args that also pin an arch, eg {'raspi32': 'arm'}
    system_name = 'Linux'      ## the OS built FOR. Copied into Toolchain.system_name
    is_cross = False           ## True when the host cannot run what this platform produces
    is_host_runnable = True    ## True when mama may run the built tests on this machine
    default_arch = ''          ## '' means use the host arch
    supported_arches = ()      ## every arch this platform accepts. The first is not special
    build_system = 'make'      ## the build system this platform prefers: make, xcode or visualstudio
    toolchain_override_attr = ''  ## BuildTarget attribute a mamafile sets to override the toolchain file
    platform_define = ''       ## 'RASPI' becomes RASPI=TRUE for the project. '' emits nothing
    compile_defines = {}       ## preprocessor defines, eg {'OCLEA':'1','YOCTO_LINUX':'1'}
    cpu_flags = {}             ## the SoC's compiler flags, eg {'-mcpu':'cortex-a53+crypto'}
    build_dirs = {}            ## arch to build dir name. An arch that is absent falls back to `name`
    cxx20_flag = 'c++20'       ## `c++2a` where the toolchain predates the final C++20 name
    ## clang dropped -dumpfullversion, so only ask a gcc-based toolchain for the full x.y.z version
    compiler_dumpfullversion = True
    ## A system library is named differently per platform: Apple links '-framework Foundation', Linux
    ## has a real file to find under /usr/lib, and everything else leaves it to the system linker.
    syslib_is_framework = False
    syslib_is_searchable = False
    ## The IDE project this platform's own generator emits, and the command that opens it. '' means
    ## mama falls back to VSCode.
    ide_project_ext = ''
    ide_project_is_dir = False
    ide_open_command = ''
    supports_coverage_report = True  ## gcovr needs gcov, which the MSVC toolchain has no equivalent of

    def __init__(self, config: BuildConfig):
        self.config = config
        self._toolchain = None


    ## --- arch ---

    def get_default_arch(self) -> str:
        return self.default_arch or host_arch()


    def arch(self) -> str:
        """The target arch, falling back to the default before BuildConfig has resolved one."""
        return self.config.arch or self.get_default_arch()


    def validate_arch(self, arch: str):
        """Raise when `arch` cannot be built for this platform. Called once, after arg parsing."""
        if self.supported_arches and arch not in self.supported_arches:
            raise RuntimeError(f'Unsupported arch={arch} on {self.name} platform!' + \
                               f' Supported={list(self.supported_arches)}')


    def system_processor(self) -> str:
        """The processor token for the CURRENT target arch. '' when the arch has no token."""
        return SYSTEM_PROCESSORS.get(self.arch(), '')


    ## --- toolchain discovery ---

    def init_toolchain(self, toolchain_dir=None, toolchain_file=None):
        """Point this platform at an explicit toolchain. A root mamafile calls this from settings().
        A native platform has nothing to find, so the default does nothing."""
        pass


    def init_default(self):
        """Find the toolchain in the default search paths. Runs after the root mamafile settings(),
        so an explicit init_toolchain() there always wins. A no-op once a toolchain is already set."""
        pass


    def toolchain(self) -> Toolchain:
        """The resolved toolchain, built once and cached. Discovery runs on the first call."""
        if self._toolchain is None:
            self.init_default()
            self._toolchain = self._build_toolchain()
        return self._toolchain


    def _build_toolchain(self) -> Toolchain:
        """Describe the resolved toolchain. A native platform names no compiler here: the compiler
        search in BuildConfig owns that choice, because the user can pick gcc or clang."""
        return Toolchain(system_name=self.system_name, system_processor=self.system_processor())


    def compiler_paths(self) -> tuple:
        """(cc, cxx, version) for a cross toolchain, or ('','','') when BuildConfig should search.
        A cross platform always answers, because a fallback to the host compiler is silent and wrong."""
        tc = self.toolchain()
        if not tc.cc: return ('', '', '')
        if not tc.version:  # one probe per run: Toolchain is cached, so this caches with it
            tc.version = self.config.get_gcc_clang_fullversion(tc.cc, self.compiler_dumpfullversion)
        return (tc.cc, tc.cxx, tc.version)


    ## --- naming ---

    def build_dir_name(self) -> str:
        """Build dir under packages/<target>/. Must be unique per (platform, arch) pair, or two
        builds clobber each other's cache."""
        return self.build_dirs.get(self.arch(), self.name)


    def distro_version(self) -> tuple:
        """(id, major, minor) for the artifactory archive name."""
        return (self.name, 0, 0)


    def banner_name(self) -> str:
        """What the build banner calls this target. The toolchain alone is ambiguous - 'clang 21.0' is
        both a host clang and the android NDK's - so the banner names the platform to prove which ran."""
        return ' '.join(p for p in (self.name, self.config.arch) if p)


    def compiler_version_tag(self) -> str:
        """Compiler id for the artifactory archive name, eg 'gcc14.3'. Named from the RESOLVED
        compiler, never from config.gcc/clang: those describe the host, so a cross build reported
        the host compiler's version for the NDK's clang."""
        cc, _, version = self.config.get_preferred_compiler_paths()
        major, minor = version.split('.')[:2]
        if 'gcc' in cc:   return f'gcc{major}.{minor}'
        if 'clang' in cc: return f'clang{major}.{minor}'
        raise EnvironmentError(f'Unrecognized compiler {cc}!')


    ## --- flags ---

    def get_cxx_flags(self, add_flag: Callable[[str, str], None]):
        """Add the compiler flags this platform always needs. `add_flag` keeps an existing value,
        so a mamafile that already set the flag wins."""
        for flag, value in self.cpu_flags.items():
            add_flag(flag, value)
        for define, value in self.compile_defines.items():
            add_flag(f'-D{define}', value)
        for path in self.toolchain().include_paths:
            add_flag(f'-I {path}')


    def get_ld_flags(self, add_ld_flag: Callable[[str, str], None]):
        """Add the linker flags this platform always needs."""
        pass


    def cxx_stdlib(self) -> str:
        """The -stdlib value for a C++ build. '' where the platform does not choose one."""
        return ''


    def make_program(self, target=None) -> str:
        """The build tool cmake drives when the generator picks none. '' lets cmake decide."""
        return ''


    def inject_env(self):
        """Set the environment variables the build tools read. Runs before configure."""
        pass


    ## --- products and tools ---

    def exe_suffix(self) -> str:
        return ''


    def lib_extensions(self) -> tuple:
        """Library file extensions this platform links. Used to filter the exported libs."""
        return ('.a', '.so')


    def gnu_host_triple(self) -> str:
        """The --host value for a GNU configure script. '' for a native build."""
        return ''


    def debugger(self) -> str:
        """'gdb', 'lldb' or '' when tests run without one."""
        return 'gdb'
