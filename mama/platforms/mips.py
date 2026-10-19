from __future__ import annotations
from typing import Callable
import os, re

from .platform import Platform, Compiler
from .toolchain import Toolchain
from mama.utils.system import System, console
from mama.utils.fileio import read_lines_from
from mama.utils.paths import path_join


class Mips(Platform):
    """MIPS cross build against an OpenWrt or a distro cross toolchain. Little endian by default,
    because that is what every consumer board runs."""
    name = 'mips'
    is_cross = True
    compiler = Compiler.GCC
    cxx20_flag = 'c++2a'  # these SDKs ship gcc older than the final C++20 name
    is_host_runnable = False
    default_arch = 'mipsel'
    supported_arches = ('mips', 'mipsel', 'mips64', 'mips64el')
    # mipsel keeps the bare `mips` dir, because it is the default and every existing package uses it.
    # Every other arch gets its own dir, so two endian variants never overwrite each other in place.
    build_dirs = {'mipsel': 'mips', 'mips': 'mipsbe', 'mips64': 'mips64', 'mips64el': 'mips64el'}
    platform_define = 'MIPS'
    compile_defines = {'MIPS': '1'}
    variant_names = ('linux',)  # every Linux triple has this field, so a plain clean keeps the dir of another triple

    def __init__(self, config):
        super().__init__(config)
        self.toolchain_major = 1
        self.toolchain_minor = 0
        self.toolchain_dir = None
        self.toolchain_file = None
        self.gnu_triple = ''    # the compilers' GNU triple, eg mipsel-linux-gnu or mips-linux-muslsf
        self.gcc_prefix = ''    # prefix to the gcc binary
        self.libs_path = ''     # toolchain lib/ path
        self.include_path = ''  # toolchain include/ path


    def compiler_prefix(self) -> str:
        """`<bin>/<triple>-`, eg `/usr/bin/mipsel-linux-gnu-`."""
        if not self.gcc_prefix: self.init_default()
        return self.gcc_prefix


    def distro_version(self) -> tuple:
        return (self.name, self.toolchain_major, self.toolchain_minor)


    def gnu_host_triple(self) -> str:
        return self.gnu_triple or f'{self.arch()}-linux-gnu'


    def variant_tokens(self) -> tuple:
        # another triple links another libc: eg mips-linux-muslsf -> ('linux', 'muslsf')
        rest = self.gnu_host_triple().lower().removeprefix(f'{self.arch()}-')
        if rest == 'linux-gnu': return ()
        return tuple(filter(None, re.split('[^a-z0-9]+', rest)))


    def init_default(self):
        if not self.gcc_prefix: self.init_toolchain()


    def init_toolchain(self, toolchain_dir=None, toolchain_file=None, arch=None, triple=None):
        """triple: the compilers' GNU triple, eg `mips-linux-muslsf` for a musl-cross-make bin/mips-linux-muslsf-gcc.
        Defaults to `<arch>-linux-gnu`. A named triple that is not found raises, it never resolves another compiler."""
        arch = arch or self.arch()
        if arch not in self.supported_arches:
            raise RuntimeError(f'Unsupported MIPS arch: {arch}')
        if toolchain_file and not os.path.exists(toolchain_file):
            raise FileNotFoundError(f'Toolchain file not found: {toolchain_file}')
        if toolchain_dir and not os.path.exists(toolchain_dir):
            raise FileNotFoundError(f'Toolchain directory not found: {toolchain_dir}')
        if not System.linux:
            raise RuntimeError('MIPS only supported on Linux')

        gnu = triple or f'{arch}-linux-gnu'
        # already initialized with exactly these inputs
        if self.gcc_prefix and (self.toolchain_file, self.toolchain_dir, self.gnu_triple) == (toolchain_file, toolchain_dir, gnu):
            return

        self.toolchain_file = toolchain_file # an optional toolchain file that adds sysroot details

        # a toolchain dir should have a bin/ subdir with the compiler
        if toolchain_dir:
            self.toolchain_dir = toolchain_dir
            if os.path.exists(f'{toolchain_dir}/bin/{gnu}-gcc'):
                self._set_toolchain_dir(gnu, f'{toolchain_dir}/bin/{gnu}-', f'{toolchain_dir}/lib', f'{toolchain_dir}/include')
                return

        # then try a system installed toolchain. Its libs may live at /usr/mipsel-linux-gnu
        if os.path.exists(f'/usr/bin/{gnu}-gcc'):
            self._set_toolchain_dir(gnu, f'/usr/bin/{gnu}-', f'/usr/{gnu}/lib', f'/usr/{gnu}/include')
            return

        if triple: raise FileNotFoundError(f'MIPS compiler not found: {toolchain_dir or "/usr"}/bin/{triple}-gcc')
        raise EnvironmentError('No MIPS toolchain compilers detected, '+
                               f'try "sudo apt-get install g++-{arch}-linux-gnu"')


    def _set_toolchain_dir(self, gnu_triple, gcc_prefix, libs_path, include_path):
        self.gnu_triple = gnu_triple
        self.gcc_prefix = gcc_prefix
        self.libs_path = libs_path if os.path.exists(libs_path) else ''
        self.include_path = include_path if os.path.exists(include_path) else ''
        self._read_linux_version()
        if self.config.print:
            console(f'Found MIPS tools: {self.gcc_prefix}gcc  linux-v{self.toolchain_major}.{self.toolchain_minor}')
            if self.libs_path:
                console(f'  MIPS syslibs: {self.libs_path}')


    def _read_linux_version(self):
        """Kernel version from the toolchain's linux/version.h. It is the only version a MIPS cross
        toolchain reports, and the artifactory archive name needs one."""
        try:
            version_file = path_join(self.include_path, 'linux/version.h')
            if not os.path.exists(version_file): return
            for line in read_lines_from(version_file):
                if line.startswith('#define LINUX_VERSION_MAJOR'):
                    self.toolchain_major = int(line.split()[2])
                elif line.startswith('#define LINUX_VERSION_PATCHLEVEL'):
                    self.toolchain_minor = int(line.split()[2])
        except:
            pass


    def _build_toolchain(self) -> Toolchain:
        prefix = self.compiler_prefix()
        return Toolchain(system_name=self.system_name, system_processor=self.system_processor(),
                         system_version='1', cc=f'{prefix}gcc', cxx=f'{prefix}g++', tool_prefix=prefix,
                         # ONLY, not NEVER: the toolchain ships its own binutils, never use the host's
                         find_root_program='ONLY',
                         toolchain_file=self.toolchain_file or '', toolchain_file_is_complete=True)


    def get_cxx_flags(self, add_flag: Callable[[str, str], None]):
        super().get_cxx_flags(add_flag)
        if self.libs_path:
            add_flag(f'-L {self.libs_path}')
