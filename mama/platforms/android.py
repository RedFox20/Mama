from __future__ import annotations
from typing import TYPE_CHECKING, Callable
import os
from mama.utils.system import System, console

if TYPE_CHECKING:
    from ..build_config import BuildConfig
    from ..build_target import BuildTarget

class Android:
    def __init__(self, config: BuildConfig):
        ## Android NDK Clang
        self.config = config
        self.toolchain_file = None  ## possibility to override the toolchain file
        self.android_sdk_path = ''
        self.android_ndk_path = ''
        self.android_api = 'android-29' # 29: Android 10.0 (2020)
        self.android_ndk_stl = 'c++_shared' # LLVM libc++
        self.ndk_version = 'ndk'


    def android_abi(self):
        if self.config.is_target_arch_armv7(): return 'armeabi-v7a'
        elif self.config.arch == 'arm64': return 'arm64-v8a'
        else: raise RuntimeError(f'Unrecognized android arch: {self.config.arch}')


    def android_home(self):
        if not self.android_sdk_path: self.init_ndk_path()
        return self.android_sdk_path


    def android_ndk(self):
        if not self.android_ndk_path: self.init_ndk_path()
        return self.android_ndk_path


    def bin(self):
        if not self.android_ndk_path: self.init_ndk_path()
        platform_dir = 'linux-x86_64'
        if System.windows: platform_dir = 'windows-x86_64'
        return f'{self.android_ndk_path}/toolchains/llvm/prebuilt/{platform_dir}/bin'


    def cc_path(self):
        platform_ver = self.android_api.replace('-', '')
        arch = 'aarch64'
        if self.config.arch == 'arm64': arch = 'aarch64'
        elif self.config.arch == 'arm': arch = 'armv7a'
        elif self.config.arch == 'x64': arch = 'x86_64'
        elif self.config.arch == 'x86': arch = 'i686'
        return f'{self.bin()}/{arch}-linux-{platform_ver}-clang'


    def cxx_path(self):
        return f'{self.cc_path()}++'


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
            paths.append(path)


    def init_ndk_path(self):
        ndk_build = 'ndk-build.cmd' if System.windows else 'ndk-build'

        # prefer NDK paths to find SDK and NDK
        ndk_paths = []
        Android._append_env(ndk_paths, 'ANDROID_NDK_LATEST_HOME')
        Android._append_env(ndk_paths, 'ANDROID_NDK_HOME')
        Android._append_env(ndk_paths, 'ANDROID_NDK_ROOT')
        Android._append_env(ndk_paths, 'ANDROID_NDK')

        for ndk_path in ndk_paths:
            if ndk_path and os.path.exists(f'{ndk_path}/{ndk_build}'):
                # figure out the Sdk path
                if os.path.exists(f'{ndk_path}/../../platforms'):
                    self._set_ndk_sdk_paths(ndk_path, os.path.abspath(f'{ndk_path}/../..'))
                    return
                elif os.path.exists(f'{ndk_path}/../platforms'):
                    self._set_ndk_sdk_paths(ndk_path, os.path.abspath(f'{ndk_path}/..'))
                    return
                else:
                    continue # try next path

        # find SDK and NDK by using SDK root
        sdk_paths = []
        Android._append_env(sdk_paths, 'ANDROID_HOME')
        Android._append_env(sdk_paths, 'ANDROID_SDK_ROOT')

        if System.windows:
            sdk_paths += [f'{os.getenv("LOCALAPPDATA")}\\Android\\Sdk']
        elif System.linux:
            sdk_paths += [
                f'{os.getenv("HOME")}/Android/Sdk',
                '/usr/bin/android-sdk',
                '/opt/android-sdk',
                '/opt/Android',
                '/Android'
            ]
        elif System.macos:
            sdk_paths += [f'{os.getenv("HOME")}/Library/Android/sdk']

        for sdk_path in sdk_paths:
            # older NDK versions with ndk-bundle subdir
            if os.path.exists(f'{sdk_path}/ndk-bundle/{ndk_build}'):
                self._set_ndk_sdk_paths(sdk_path + '/ndk-bundle', sdk_path)
                return
            # newer NDK with multiple ndk versions:
            elif os.path.exists(f'{sdk_path}/ndk'):
                subdirs = os.listdir(f'{sdk_path}/ndk')
                subdirs.sort(reverse=True)
                for subdir in subdirs:
                    if os.path.exists(f'{sdk_path}/ndk/{subdir}/{ndk_build}'):
                        self.ndk_version = subdir
                        self._set_ndk_sdk_paths(f'{sdk_path}/ndk/{subdir}', sdk_path)
                        return
        raise EnvironmentError(f'''Could not detect any Android NDK installations. 
Default search paths: {ndk_paths+sdk_paths} 
Define env ANDROID_NDK_HOME with path to the preferred NDK installation
Or define env ANDROID_HOME with path to Android SDK root with valid NDK-s.''')


    def get_cxx_flags(self, add_flag: Callable[[str,str], None]):
        if self.config.is_target_arch_armv7():
            add_flag('-march', 'armv7-a')
            add_flag('-mfpu', 'neon')
        else:
            add_flag('-march', 'armv8-a')

        # this was only needed for legacy NDK toolchains
        # but on latest toolchains these issues are fixed
        # add_flag(f'-I"{self.android_ndk()}/toolchains/llvm/prebuilt/linux-x86_64/sysroot/usr/include"')
        # add_flag(f'-I"{self.android_ndk()}/sources/cxx-stl/llvm-libc++/include"')


    def _get_make(self):
        if System.windows:
            return f'{self.android_ndk()}\\prebuilt\\windows-x86_64\\bin\\make.exe' # CodeBlocks - Unix Makefiles
        elif System.macos:
            return f'{self.android_ndk()}/prebuilt/darwin-x86_64/bin/make' # CodeBlocks - Unix Makefiles
        return ''


    def _get_toolchain_path(self, target: BuildTarget) -> str:
        if target.cmake_ndk_toolchain:
            if os.path.isabs(target.cmake_ndk_toolchain):
                toolchain = target.cmake_ndk_toolchain
            else:
                toolchain = target.source_dir(target.cmake_ndk_toolchain)
            if os.path.exists(toolchain):
                return toolchain

        if self.toolchain_file and os.path.exists(self.toolchain_file):
            return self.toolchain_file

        toolchain = f'{self.android_ndk()}/build/cmake/android.toolchain.cmake'
        if os.path.exists(toolchain):
            return toolchain
        return ''


    def get_cmake_build_opts(self, target: BuildTarget) -> list:
        opts = [
            'CMAKE_SYSTEM_NAME=Android',
            f'ANDROID_ABI={self.android_abi()}',
            'ANDROID_ARM_NEON=TRUE',
            f'ANDROID_NDK="{self.android_ndk()}"',
            f'ANDROID_STL={self.android_ndk_stl}',
            f'ANDROID_NATIVE_API_LEVEL={self.android_api}',
            'ANDROID_TOOLCHAIN=clang',
            'CMAKE_BUILD_WITH_INSTALL_RPATH=ON',
            'ANDROID_USE_LEGACY_TOOLCHAIN_FILE=FALSE'
        ]

        # get the toolchain file overriden by build target
        target_toolchain = target.source_dir(target.cmake_ndk_toolchain)
        if target.cmake_ndk_toolchain and os.path.exists(target_toolchain):
            toolchain = target_toolchain
        elif self.toolchain_file and os.path.exists(self.toolchain_file):
            toolchain = self.toolchain_file
        else:
            toolchain = f'{self.android_ndk()}/build/cmake/android.toolchain.cmake'

        if toolchain:
            opts.append(f'CMAKE_TOOLCHAIN_FILE="{toolchain}"')
            if self.config.print:
                console(f'Toolchain: {toolchain}')

        make = self._get_make()
        if make: opts.append(f'CMAKE_MAKE_PROGRAM="{make}"')
        return opts

    # injects android specific env vars
    def inject_env(self):
        make = self._get_make()
        if make: os.environ['CMAKE_MAKE_PROGRAM'] = make
        os.environ['ANDROID_HOME'] = self.android_home()
        os.environ['ANDROID_NDK'] = self.android_ndk()
        os.environ['ANDROID_ABI'] = self.android_abi()
        os.environ['ANDROID_STL'] = self.android_ndk_stl
        os.environ['ANDROID_NATIVE_API_LEVEL'] = self.android_api
        os.environ['ANDROID_TOOLCHAIN'] = 'clang'
