from __future__ import annotations
import glob, os, re, sys

from .platform import Platform, Compiler
from .toolchain import Toolchain
from mama.utils.fileio import find_executable_from_system
from mama.utils.paths import forward_slashes, normalized_path, quoted
from mama.utils.system import System, console, warning


# The emscripten dir inside an emsdk install, and the CMake toolchain file inside the emscripten dir
_EMSDK_EMSCRIPTEN = 'upstream/emscripten'
_TOOLCHAIN_FILE = 'cmake/Modules/Platform/Emscripten.cmake'


def _version_key(path: str) -> list:
    """The numbers in a path, so that `node/22.16.0` sorts after `node/8.9.1`."""
    return [int(n) for n in re.findall(r'\d+', path)]


class Wasm(Platform):
    """WebAssembly cross build with the Emscripten SDK: emcc, em++ and the CMake toolchain file of the SDK.
    node runs a program this platform links, so `mama test` works on every host."""
    name = 'wasm'
    system_name = 'Emscripten'
    is_cross = True
    default_arch = 'wasm32'
    supported_arches = ('wasm32',)
    build_system = 'ninja'            # the SDK ships no make program, and a Windows host has none
    compiler = Compiler.CLANG
    compiler_dumpfullversion = False  # emcc supports -dumpversion only
    supports_march = False            # clang has no -march for the wasm32 target
    position_independent = False      # a wasm program links no shared library
    program_suffix = '.js'            # emcc writes <name>.js, which loads <name>.wasm
    exceptions_flag = '-fwasm-exceptions'

    def __init__(self, config):
        super().__init__(config)
        self.emscripten = ''  ## the emscripten dir: emcc, em++ and the CMake toolchain file
        self.emsdk = ''       ## the emsdk install that holds the emscripten dir, '' for a standalone one
        self.node = ''        ## the node that launcher() found


    def init_toolchain(self, toolchain_dir=None, toolchain_file=None):
        """Use an explicit SDK. The toolchain file is always the one inside it.
        toolchain_dir: an emsdk root, or the emscripten dir inside it
        """
        if toolchain_dir and not self._use_sdk(toolchain_dir):
            raise RuntimeError(f'No Emscripten SDK at {toolchain_dir}: it holds no {_TOOLCHAIN_FILE}.')
        if toolchain_file:
            warning(f'wasm ignores the toolchain file {toolchain_file}. It uses the {_TOOLCHAIN_FILE} of the SDK.')
        self.init_default()


    def init_default(self):
        if self.emscripten: return
        emcc = find_executable_from_system('emcc', follow_symlinks=True)
        searched = [p for p in (os.getenv('EMSDK'), os.path.expanduser('~/emsdk'), emcc and os.path.dirname(emcc)) if p]
        for path in searched:
            if self._use_sdk(path): return
        raise EnvironmentError(f'No Emscripten SDK found. Searched: {searched}. Set env EMSDK to an emsdk' + \
                               ' install, or put emcc on PATH.')


    def _use_sdk(self, path: str) -> bool:
        """Use the SDK at `path`, an emsdk root or an emscripten dir. False when it holds neither."""
        path = normalized_path(path)
        for emscripten in (f'{path}/{_EMSDK_EMSCRIPTEN}', path):
            if not os.path.exists(f'{emscripten}/{_TOOLCHAIN_FILE}'): continue
            self.emscripten = emscripten
            emsdk = os.path.dirname(os.path.dirname(emscripten))
            self.emsdk = emsdk if os.path.exists(f'{emsdk}/emsdk.py') else ''
            if self.config.print: console(f'Found Emscripten SDK: {emscripten}')
            return True
        return False


    def _tool(self, name: str) -> str:
        """An emscripten tool, eg `emcc`. A Windows host has `emcc.exe`, or `emcc.bat` in an older SDK."""
        self.init_default()
        self.inject_env()  # every emscripten tool runs on Python, also the version probe of emcc
        path = f'{self.emscripten}/{name}'
        if not System.windows: return path
        return f'{path}.exe' if os.path.exists(f'{path}.exe') else f'{path}.bat'


    def _build_toolchain(self) -> Toolchain:
        cc, cxx = self._tool('emcc'), self._tool('em++')
        return Toolchain(system_name=self.system_name, system_processor=self.system_processor(), cc=cc, cxx=cxx,
                         toolchain_file=f'{self.emscripten}/{_TOOLCHAIN_FILE}', toolchain_file_is_complete=True)


    def archiver(self) -> str:
        return self._tool('emar')


    def get_ld_flags(self, add_ld_flag):
        # every link gets the flag, so a target without exceptions still links a library that throws
        add_ld_flag(self.exceptions_flag)


    def lib_extensions(self) -> tuple:
        return ('.a',)


    def debugger(self) -> str:
        return ''


    def gcov_command(self) -> str:
        """`llvm-cov gcov` of the emsdk install: emcc writes the coverage format of that LLVM. '' without emsdk."""
        self.init_default()
        llvm_cov = f'{self.emsdk}/upstream/bin/llvm-cov' + ('.exe' if System.windows else '')
        return f'{quoted(llvm_cov)} gcov' if self.emsdk and os.path.exists(llvm_cov) else ''


    def make_program(self, target=None) -> str:
        """Ninja, also for a target that disables it: the SDK ships no make program."""
        return self.config.ninja_path


    def launcher(self) -> str:
        """EMSDK_NODE, else the newest node of the emsdk install, else the node on PATH."""
        if self.node: return self.node
        self.init_default()
        pattern = 'node/*/node.exe' if System.windows else 'node/*/bin/node'
        bundled = max(glob.glob(f'{self.emsdk}/{pattern}'), key=_version_key, default='') if self.emsdk else ''
        node = os.getenv('EMSDK_NODE') or bundled or find_executable_from_system('node')
        if not node:
            raise EnvironmentError('No node found to run a wasm program. Install node, or set env EMSDK_NODE.')
        self.node = quoted(forward_slashes(node))
        return self.node


    def inject_env(self):
        # emcc runs on Python 3.10 or newer, which mama itself requires
        os.environ.setdefault('EMSDK_PYTHON', sys.executable)
