from __future__ import annotations
import os
from platform import version as _os_version  # stdlib platform, NOT mama.platforms.platform
from .platform import Platform
from .toolchain import Toolchain
from mama.util import find_executable_from_system, path_join
from mama.utils.system import System, console
from mama.utils.sub_process import execute_piped


# Where Visual Studio installs itself, newest first. vswhere.exe answers this properly, so these are
# only the fallback for a machine where the installer is missing.
VS_VARIANTS = ('Enterprise', 'Professional', 'Community')
VS_ROOTS = [f'C:\\Program Files\\Microsoft Visual Studio\\{v}' for v in ('18', '2022')] + \
            [f'C:\\Program Files (x86)\\Microsoft Visual Studio\\{v}' for v in ('2019', '2017')]

# The generator name cmake and the VS installer agree on, keyed by the version dir in the install path.
_VS_GENERATORS = {'\\18\\': 'Visual Studio 18 2026', '\\2022\\': 'Visual Studio 17 2022',
                  '\\2019\\': 'Visual Studio 16 2019'}
_VS_GENERATOR_FALLBACK = 'Visual Studio 15 2017'

# mama arch to the name Visual Studio and MSBuild call it. Note Win32, not x86.
_VS_ARCHES = {'x64': 'x64', 'x86': 'Win32', 'arm': 'ARM', 'arm64': 'ARM64'}

_found = {}  # discovery results, memoized for the process: a VS install cannot move mid-run


def _memo(key, find):
    if key not in _found: _found[key] = find()
    return _found[key]


def find_vswhere(fail_on_error=True) -> str:
    """vswhere.exe, which reports where Visual Studio is installed. '' when it is missing."""
    def find():
        installer = 'C:\\Program Files (x86)\\Microsoft Visual Studio\\Installer\\vswhere.exe'
        if os.path.exists(installer): return installer
        return find_executable_from_system('vswhere.exe') or ''
    path = _memo('vswhere', find)
    if not path and fail_on_error:
        raise EnvironmentError('Failed to find vswhere.exe for detecting Visual Studio installations!' \
                               'Please install Visual Studio with C++ workload and try again.')
    return path


def vswhere_property(name: str) -> str:
    """Ask vswhere for a property of the newest install, eg installationPath. '' when it cannot."""
    vswhere = find_vswhere(fail_on_error=False)
    return execute_piped(f'"{vswhere}" -latest -nologo -property {name}') if vswhere else ''


def find_visualstudio(verbose=False) -> str:
    """The Visual Studio install root. Raises when there is none: every MSVC path derives from it."""
    def find():
        # inside the memo, so mama reports the detection once. A print per caller wrote the same line
        # 8 times into one verbose build log.
        vspath = vswhere_property('installationPath')
        if not vspath or not os.path.exists(vspath):
            variants = [f'{root}\\{v}' for root in VS_ROOTS for v in VS_VARIANTS]
            vspath = next((p for p in variants if os.path.exists(p)), '')
        if vspath and verbose: console(f'Detected VisualStudio: {vspath}')
        return vspath
    if not System.windows:
        raise EnvironmentError('VisualStudio tools support not available on this platform!')
    path = _memo('visualstudio', find)
    if not path:
        raise EnvironmentError('Failed to find Visual Studio installation!' \
                               ' Please install Visual Studio with C++ workload and try again.')
    return path


def latest_msvc_toolset(tools_root: str) -> str:
    """Newest MSVC toolset (highest version) that still has the x64 cl.exe. An upgrade can leave the old
    version's dir behind without binaries, and os.listdir order is unspecified - so sort by version
    (numerically, not lexically: 14.51 > 14.9) and skip toolsets whose cl.exe is gone. '' if none:
    every msvc_* path is bin/Hostx64/x64, so a toolset without it only defers the failure."""
    try:
        dirs = [d for d in os.listdir(tools_root) if os.path.isdir(os.path.join(tools_root, d))]
    except OSError:
        return ''
    dirs.sort(key=lambda n: tuple(int(p) if p.isdigit() else 0 for p in n.split('.')), reverse=True)
    for d in dirs:
        if os.path.isfile(os.path.join(tools_root, d, 'bin', 'Hostx64', 'x64', 'cl.exe')):
            return path_join(tools_root, d)
    return ''  # a dir without cl.exe can't build; '' lets the caller say so up front


class Windows(Platform):
    """MSVC on Windows. Not a cross build: the toolset and the Windows SDK pick the compiler, so
    mama names no compiler path and resolves the toolset through vswhere instead."""
    name = 'windows'
    cli_aliases = ('msvc',)
    system_name = 'Windows'
    build_system = 'visualstudio'
    supported_arches = tuple(_VS_ARCHES)
    build_dirs = {'x64': 'windows', 'x86': 'windows32', 'arm64': 'winarm', 'arm': 'winarm32'}
    ide_project_ext = ('.slnx', '.sln')  # VS 18 (2026) writes the XML .slnx, an older toolset writes .sln
    ide_open_command = 'start'
    supports_coverage_report = False

    def _build_toolchain(self) -> Toolchain:
        # An x86 target needs the 32-bit host toolset. Everything else about the compiler comes from
        # the toolset and the Windows SDK, so mama names no compiler path here.
        return Toolchain(system_name=self.system_name, host_toolset='x86' if self.arch() == 'x86' else '')


    def distro_version(self) -> tuple:
        version = _os_version().split('.') + ['0']
        return (self.name, int(version[0]), int(version[1]))


    def compiler_version_tag(self) -> str:
        return 'msvc' + os.path.basename(self.msvc_tools_path().rstrip('\\//')).split('.')[0]


    ## --- Visual Studio and the MSVC toolset ---

    def visualstudio_path(self) -> str:
        """ eg "C:\\Program Files\\Microsoft Visual Studio\\2022\\Community" """
        return find_visualstudio(self.config.verbose)


    def generator_name(self) -> str:
        """The Visual Studio generation, eg 'Visual Studio 17 2022'. Both cmake and mama name it this."""
        vspath = self.visualstudio_path()
        return next((g for tag, g in _VS_GENERATORS.items() if tag in vspath), _VS_GENERATOR_FALLBACK)


    def generator_arch(self) -> str:
        """The target arch as Visual Studio and MSBuild name it, eg 'Win32' for x86."""
        arch = _VS_ARCHES.get(self.arch())
        if not arch: raise RuntimeError(f'Unsupported arch: {self.config.arch}')
        return arch


    def msvc_tools_path(self) -> str:
        """ MSVC tools at, for example: "{VisualStudioPath}\\VC\\Tools\\MSVC\\14.16.27023" """
        def find():  # inside the memo, so the detection prints once, not once per caller
            toolset = latest_msvc_toolset(f'{self.visualstudio_path()}\\VC\\Tools\\MSVC')
            if toolset and self.config.verbose: console(f'Detected MSVC Tools: {toolset}')
            return toolset
        path = _memo('msvctools', find)
        if not path: raise EnvironmentError('Could not detect MSVC Tools')
        return path


    def msvc_bin64(self) -> str: return f'{self.msvc_tools_path()}/bin/Hostx64/x64/'
    def msvc_cl64(self) -> str:   return f'{self.msvc_bin64()}cl.exe'
    def msvc_link64(self) -> str: return f'{self.msvc_bin64()}link.exe'
    def msvc_lib64(self) -> str:  return f'{self.msvc_tools_path()}\\lib\\x64'


    ## --- products and tools ---

    def exe_suffix(self) -> str:
        return '.exe'


    def lib_extensions(self) -> tuple:
        return ('.lib',)


    def debugger(self) -> str:
        return ''  # the test exe runs directly, there is no batch-mode debugger to wrap it
