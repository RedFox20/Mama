from __future__ import annotations
from typing import Callable
import os

from .platform import Platform
from .toolchain import Toolchain
from mama.util import path_join
from mama.utils.system import System, console, warning
from mama import util


# mama arch to the NDK's own tokens: the clang driver name and the ABI dir name.
_NDK_ARCH = {'arm64': 'aarch64', 'arm': 'armv7a', 'x64': 'x86_64', 'x86': 'i686'}
_NDK_ABI = {'arm64': 'arm64-v8a', 'arm': 'armeabi-v7a'}


class Android(Platform):
    """Android NDK cross build with the NDK's own clang and its CMake toolchain file."""
    name = 'android'
    system_name = 'Android'
    is_cross = True
    is_host_runnable = False
    default_arch = 'arm64'
    supported_arches = ('arm64', 'arm')
    build_dirs = {'arm64': 'android', 'arm': 'android32'}
    toolchain_override_attr = 'cmake_ndk_toolchain'
    compiler_dumpfullversion = False  # the NDK ships clang, which dropped -dumpfullversion

    def __init__(self, config):
        super().__init__(config)
        self.toolchain_file = None  ## explicit toolchain file, set by a root mamafile. None uses the NDK's own
        self.android_sdk_path = ''
        self.android_ndk_path = ''
        self.android_api = 'android-29' # API 29: Android 10 (2019)
        self.android_ndk_stl = 'c++_shared' # LLVM libc++
        self.ndk_version = ''


    def android_abi(self):
        abi = _NDK_ABI.get(self.arch())
        if not abi: raise RuntimeError(f'Unrecognized android arch: {self.config.arch}')
        return abi


    def android_home(self):
        """ /opt/android-sdk/ """
        if not self.android_sdk_path: self.init_ndk_path()
        return self.android_sdk_path


    def android_ndk(self):
        """ /opt/android-sdk/ndk/26.1.10909125 """
        if not self.android_ndk_path: self.init_ndk_path()
        return self.android_ndk_path


    def bin(self):
        """ /opt/android-sdk/ndk/26.1.10909125/toolchains/llvm/prebuilt/linux-x86_64/bin """
        platform_dir = 'windows-x86_64' if System.windows else 'linux-x86_64'
        return f'{self.android_ndk()}/toolchains/llvm/prebuilt/{platform_dir}/bin'


    def _clang_path(self, suffix: str) -> str:
        """`<ndk bin>/aarch64-linux-android29-clang`, or the `clang++` variant."""
        arch = _NDK_ARCH.get(self.config.arch, 'aarch64')
        ext = '.cmd' if System.windows else ''
        return f'{self.bin()}/{arch}-linux-{self.android_api.replace("-", "")}-{suffix}{ext}'


    def cc_path(self):
        return self._clang_path('clang')


    def cxx_path(self):
        return self._clang_path('clang++')


    def distro_version(self) -> tuple:
        return (self.name, int(self.android_api.split('-')[1]), 0)


    def banner_name(self) -> str:
        ndk = os.path.basename(self.android_ndk().rstrip('/\\'))
        # android_api already contains the platform name, eg 'android-29'
        return ' '.join(p for p in (self.android_api, self.config.arch, f'ndk-{ndk}' if ndk else '') if p)


    def init_default(self):
        if not self.android_ndk_path: self.init_ndk_path()


    def init_toolchain(self, toolchain_dir=None, toolchain_file=None):
        """Use an explicit NDK CMake toolchain file. The NDK itself is still discovered, because
        the toolchain file alone does not name the clang binaries.
        toolchain_file: path to the NDK CMake toolchain file to use
        """
        if toolchain_file: self.set_toolchain_path(toolchain_file)
        self.init_default()


    def set_toolchain_path(self, toolchain_file: str):
        if not os.path.exists(toolchain_file):
            raise RuntimeError(f'Android toolchain file not found: {toolchain_file}')
        self.toolchain_file = toolchain_file


    def _set_ndk_sdk_paths(self, ndk_path: str, sdk_path: str):
        self.android_sdk_path = sdk_path
        self.android_ndk_path = ndk_path
        if self.config.print:
            console(f'Found Android SDK: {self.android_sdk_path}')
            console(f'Found Android NDK: {self.android_ndk_path}')


    @staticmethod
    def _append_env(paths:list, env: str):
        path = os.getenv(env)
        if path and os.path.exists(path):
            paths.append(util.forward_slashes(path))


    def init_ndk_path(self):
        ndk_build = 'ndk-build.cmd' if System.windows else 'ndk-build'

        # find both paths from an NDK env var first
        ndk_paths = []
        if not self.ndk_version: # an explicit ndk-<ver> argument pins the version, so skip the env var search
            Android._append_env(ndk_paths, 'ANDROID_NDK_LATEST_HOME')
            Android._append_env(ndk_paths, 'ANDROID_NDK_HOME')
            Android._append_env(ndk_paths, 'ANDROID_NDK_ROOT')
            Android._append_env(ndk_paths, 'ANDROID_NDK')

            for ndk_path in ndk_paths:
                if ndk_path and os.path.exists(f'{ndk_path}/{ndk_build}'):
                    # derive the SDK root from the NDK path, usually <SDK>/ndk/26.1.10909125 -> <SDK>
                    sdk_path = os.getenv('ANDROID_HOME') or os.getenv('ANDROID_SDK_ROOT')
                    if not sdk_path and os.path.exists(f'{ndk_path}/../../platforms'):
                        sdk_path = util.forward_slashes(os.path.abspath(f'{ndk_path}/../..'))
                    if not sdk_path and os.path.exists(f'{ndk_path}/../platforms'):
                        sdk_path = util.forward_slashes(os.path.abspath(f'{ndk_path}/..'))
                    if not sdk_path: # default to the modern layout <SDK>/ndk/<version>
                        sdk_path = util.forward_slashes(os.path.abspath(f'{ndk_path}/../..'))
                    self._set_ndk_sdk_paths(ndk_path, sdk_path)
                    return

        # otherwise scan the known SDK roots for an NDK
        sdk_paths = []
        Android._append_env(sdk_paths, 'ANDROID_HOME')
        Android._append_env(sdk_paths, 'ANDROID_SDK_ROOT')

        if os.getenv("HOME"):
            user_sdk = util.forward_slashes(os.path.expanduser('~/Android/Sdk'))
            if os.path.exists(user_sdk):
                sdk_paths += [user_sdk]

        if System.windows:
            localappdata = util.forward_slashes(os.getenv("LOCALAPPDATA"))
            if localappdata:
                sdk_paths += [f'{localappdata}/Android/Sdk']
        elif System.linux:
            sdk_paths += [
                '/usr/bin/android-sdk',
                '/opt/android-sdk',
                '/opt/Android',
                '/Android'
            ]
        elif System.macos:
            if os.getenv("HOME"):
                sdk_paths += [os.path.expanduser('~/Library/Android/sdk')]

        for sdk_path in sdk_paths:
            # older NDK versions with ndk-bundle subdir
            if os.path.exists(f'{sdk_path}/ndk-bundle/{ndk_build}'):
                self._set_ndk_sdk_paths(sdk_path + '/ndk-bundle', sdk_path)
                return
            # newer NDK layout with multiple versions under ndk/
            elif os.path.exists(f'{sdk_path}/ndk'):
                subdirs = os.listdir(f'{sdk_path}/ndk')
                subdirs.sort(reverse=True) # newest version first
                for subdir in subdirs:
                    if self.ndk_version and not subdir.startswith(self.ndk_version):
                        if self.config.verbose:
                            warning(f'Skipping NDK version {subdir} since it does not match the requested version {self.ndk_version}')
                        continue
                    if os.path.exists(f'{sdk_path}/ndk/{subdir}/{ndk_build}'):
                        self.ndk_version = subdir
                        self._set_ndk_sdk_paths(f'{sdk_path}/ndk/{subdir}', sdk_path)
                        return
        raise EnvironmentError(f'''Could not detect any Android NDK installations.
Default search paths: {ndk_paths+sdk_paths}
Define env ANDROID_NDK_HOME with path to the preferred NDK installation
Or define env ANDROID_HOME with path to Android SDK root with valid NDK-s.''')


    def _build_toolchain(self) -> Toolchain:
        # variables only the NDK's own toolchain file understands, so they take the escape hatch
        ndk = (f'ANDROID_ABI={self.android_abi()}', 'ANDROID_ARM_NEON=TRUE', 'ANDROID_TOOLCHAIN=clang',
               f'ANDROID_ARCH={"ARM64" if self.arch() == "arm64" else "arm"}',
               f'ANDROID_NDK="{self.android_ndk()}"', f'ANDROID_STL={self.android_ndk_stl}',
               f'ANDROID_NATIVE_API_LEVEL={self.android_api}', 'ANDROID_USE_LEGACY_TOOLCHAIN_FILE=FALSE')
        return Toolchain(system_name=self.system_name, system_processor=self.system_processor(),
                         cc=self.cc_path(), cxx=self.cxx_path(), install_rpath=True,
                         toolchain_file=self._toolchain_path(), extra_opts=ndk)


    def get_cxx_flags(self, add_flag: Callable[[str,str], None]):
        if self.arch() == 'arm':
            add_flag('-march', 'armv7-a')
            add_flag('-mfpu', 'neon')
        else:
            add_flag('-march', 'armv8-a')
        super().get_cxx_flags(add_flag)


    def make_program(self, target=None) -> str:
        """The NDK ships its own make for hosts that have none. Linux always has one, so '' there
        lets the build system find it. ONLY this place names a make program, or cmake gets it twice."""
        if System.windows: platform_dir = 'windows-x86_64'
        elif System.macos: platform_dir = 'darwin-x86_64'
        else: return ''
        return path_join(self.android_ndk(), 'prebuilt', platform_dir, 'bin', 'make')


    def _toolchain_path(self) -> str:
        """The NDK toolchain file: a mamafile override, else the one the NDK ships. A per-target
        override is resolved by the build system, through `toolchain_override_attr`."""
        if self.toolchain_file and os.path.exists(self.toolchain_file):
            return self.toolchain_file
        toolchain = f'{self.android_ndk()}/build/cmake/android.toolchain.cmake'
        return toolchain if os.path.exists(toolchain) else ''


    def inject_env(self):
        os.environ['ANDROID_HOME'] = self.android_home()
        os.environ['ANDROID_NDK'] = self.android_ndk()
        os.environ['ANDROID_ABI'] = self.android_abi()
        os.environ['ANDROID_STL'] = self.android_ndk_stl
        os.environ['ANDROID_NATIVE_API_LEVEL'] = self.android_api
        os.environ['ANDROID_TOOLCHAIN'] = 'clang'
