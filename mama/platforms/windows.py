from __future__ import annotations
import os
from functools import lru_cache
from platform import version as _os_version  # stdlib platform, NOT mama.platforms.platform
from .platform import Platform, host_arch
from .toolchain import Toolchain
from mama.utils.fileio import find_executable_from_system
from mama.utils.paths import path_join
from mama.utils.system import System, console
from mama.utils.sub_process import execute_piped


@lru_cache(maxsize=1)
def emulates_x64() -> bool:
    """True on a Windows that runs an x64 binary on an arm64 host. Windows 11 added that emulator, at
    build 22000. Windows 10 on arm runs an x86 binary and nothing wider."""
    build = _os_version().split('.')
    try: return int(build[2]) >= 22000
    except (IndexError, ValueError): return False


# Visual Studio install roots, newest first. Only the fallback for a machine with no vswhere.exe.
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
        # inside the memo, so the detection prints once, not once per caller
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
    """Newest MSVC toolset dir that still has the x64 cl.exe, or '' when none does. An upgrade can
    leave a version dir behind without binaries, so sort by version, numerically because 14.51 > 14.9,
    and skip a toolset whose cl.exe is gone. Every msvc_* path assumes bin/Hostx64/x64.
    tools_root: the `VC\\Tools\\MSVC` dir that holds the versioned toolsets
    """
    try:
        dirs = [d for d in os.listdir(tools_root) if os.path.isdir(os.path.join(tools_root, d))]
    except OSError:
        return ''
    dirs.sort(key=lambda n: tuple(int(p) if p.isdigit() else 0 for p in n.split('.')), reverse=True)
    for d in dirs:
        if os.path.isfile(os.path.join(tools_root, d, 'bin', 'Hostx64', 'x64', 'cl.exe')):
            return path_join(tools_root, d)
    return ''


def msvc_toolset_version(tools_path: str) -> str:
    """Major and minor of an MSVC toolset dir, eg '14.51' for `.../MSVC/14.51.36231`. The patch field
    changes with every Visual Studio update and does not change the ABI, so no mama name carries it."""
    return '.'.join(os.path.basename(tools_path.rstrip('\\/')).split('.')[:2])


class Windows(Platform):
    """MSVC on Windows. Not a cross build. The toolset and the Windows SDK pick the compiler, so
    mama names no compiler path and resolves the toolset through vswhere instead."""
    name = 'windows'
    cli_aliases = ('msvc',)
    system_name = 'Windows'
    build_system = 'visualstudio'
    supported_arches = tuple(_VS_ARCHES)
    build_dirs = {'x64': 'windows', 'x86': 'windows32', 'arm64': 'winarm', 'arm': 'winarm32'}
    also_runs = {'x64': ('x86',), 'arm64': ('x64', 'x86')}  ## an arm64 host emulates both, see runs_on_host
    ide_project_ext = ('.slnx', '.sln')  # VS 18 (2026) writes the XML .slnx. An older toolset writes .sln
    ide_open_command = 'start'
    supports_coverage_report = False
    supports_march = False  # MSVC has no -march. Its nearest flag, /arch:, names a different axis

    def _build_toolchain(self) -> Toolchain:
        # An x86 target needs the 32-bit host toolset. The toolset and the SDK pick the compiler, so mama names none.
        return Toolchain(system_name=self.system_name, host_toolset='x86' if self.arch() == 'x86' else '')


    def runs_on_host(self, arch: str) -> bool:
        """The arm64 x64 emulator is a Windows 11 feature, so an older arm64 host runs x86 alone."""
        if arch == 'x64' and host_arch() == 'arm64': return emulates_x64()
        return super().runs_on_host(arch)


    def distro_version(self) -> tuple:
        version = _os_version().split('.') + ['0']
        return (self.name, int(version[0]), int(version[1]))


    def compiler_version_tag(self) -> str:
        """Compiler id for the artifactory archive name, eg 'msvc14.51' for toolset 14.51.36231. Same shape
        as 'gcc14.3'. The major alone tagged every toolset since 2015 alike, so an upgrade reused its package."""
        return 'msvc' + msvc_toolset_version(self.msvc_tools_path())


    ## --- Visual Studio and the MSVC toolset ---

    def visualstudio_path(self) -> str:
        """ eg "C:\\Program Files\\Microsoft Visual Studio\\2022\\Community" """
        return find_visualstudio(self.config.verbose)


    def generator_name(self) -> str:
        """The cmake generator name, eg 'Visual Studio 17 2022', matched from the install path."""
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


    def list_archive_members_cmd(self, lib: str) -> list:
        return ['lib.exe', '/NOLOGO', '/LIST', lib]


    def remove_from_archive_cmd(self, lib: str, members: list) -> list:
        return ['lib.exe', '/NOLOGO', lib, *[f'/REMOVE:{m}' for m in members]]


    def debugger(self) -> str:
        return ''  # no batch-mode debugger exists here, so the test exe runs directly
