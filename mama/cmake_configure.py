import os, subprocess, shlex
from mama.system import System, console, execute
from mama.build_config import BuildConfig
import threading
from queue import Queue

class AsyncFileReader:
    def __init__(self, file):
        self.file = file
        self.queue = Queue()
        self.thread = threading.Thread(target=self._read_thread)
        self.keep_polling = True
        self.thread.daemon = True
        self.thread.start()
    
    def _read_thread(self):
        while self.keep_polling:
            self.queue.put(self.file.readline())

    def readline(self):
        if self.queue.empty():
            return ''
        return self.queue.get()

    def stop(self):
        self.keep_polling = False
        self.thread.join()


def rerunnable_cmake_conf(cwd, args, allow_rerun):
    rerun = False
    error = ''
    proc = subprocess.Popen(args, shell=True, universal_newlines=True, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    output = AsyncFileReader(proc.stdout)
    errors = AsyncFileReader(proc.stderr)
    while True:
        if proc.poll() is None:
            line = output.readline()
            if line: console(line.rstrip())

            error = errors.readline()
            if error:
                error = error.rstrip()
                console(error)
                # this happens every time MSVC compiler is updated. simple fix is to rerun cmake
                if System.windows:
                    rerun |= error.startswith('  is not a full path to an existing compiler tool.')
        else:
            output.stop()
            errors.stop()
            if proc.returncode == 0:
                break
            if rerun:
                return rerunnable_cmake_conf(cwd, args, False)
            raise Exception(f'CMake configure error: {error}')


def run_cmake_config(target, cmake_flags):
    generator = cmake_generator(target)
    src_dir = target.dep.src_dir
    cmd = f'cmake {generator} {cmake_flags} -DCMAKE_INSTALL_PREFIX="." "{src_dir}"'
    rerunnable_cmake_conf(target.dep.build_dir, cmd, True)


def run_cmake_build(target, extraflags=''):
    build_dir = target.dep.build_dir
    flags = cmake_build_config(target)
    execute(f'cmake --build {build_dir} {flags} {extraflags}')


def cmake_generator(target):
    config:BuildConfig = target.config
    if target.enable_unix_make:   return '-G "CodeBlocks - Unix Makefiles"'
    if config.windows:            return '-G "Visual Studio 15 2017 Win64"'
    if target.enable_ninja_build: return '-G "Ninja"'
    if config.android:            return '-G "CodeBlocks - Unix Makefiles"'
    if config.linux:              return '-G "CodeBlocks - Unix Makefiles"'
    if config.ios:                return '-G "Xcode"'
    if config.macos:              return '-G "Xcode"'
    else:                         return ''


def cmake_make_program(target):
    config:BuildConfig = target.config
    if config.windows:     return ''
    if target.enable_unix_make:   return ''
    if target.enable_ninja_build: return config.ninja_path
    if config.android:
        if System.windows:
            return f'{config.ndk_path}\\prebuilt\\windows-x86_64\\bin\\make.exe' # CodeBlocks - Unix Makefiles
        elif System.macos:
            return f'{config.ndk_path}/prebuilt/darwin-x86_64/bin/make' # CodeBlocks - Unix Makefiles
    return ''


def cmake_default_options(target):
    config:BuildConfig = target.config
    cxxflags = target.cmake_cxxflags
    ldflags  = target.cmake_ldflags
    exceptions = target.enable_exceptions
    if config.windows:
        cxxflags += ' /EHsc -D_HAS_EXCEPTIONS=1' if exceptions else ' -D_HAS_EXCEPTIONS=0'
        cxxflags += ' -DWIN32=1' # so yeah, only _WIN32 is defined by default, but opencv wants to see WIN32
        cxxflags += ' /MP'
    else:
        cxxflags += '' if exceptions else ' -fno-exceptions'
    
    if config.android and config.android_ndk_stl == 'c++_shared':
        cxxflags += f' -I"{config.ndk_path}/sources/cxx-stl/llvm-libc++/include"'
    elif config.linux:
        cxxflags += ' -march=native'
        if config.clang:
            cxxflags += ' -stdlib=libc++'
    elif config.macos:
        cxxflags += ' -march=native -stdlib=libc++'
    elif config.ios:
        cxxflags += f' -arch arm64 -stdlib=libc++ -miphoneos-version-min={config.ios_version}'

    opt = ["CMAKE_POSITION_INDEPENDENT_CODE=ON"]
    if config.linux:
        if config.gcc:
            opt += ['CMAKE_C_COMPILER=gcc', 'CMAKE_CXX_COMPILER=g++']
        elif config.clang:
            opt += ['CMAKE_C_COMPILER=clang', 'CMAKE_CXX_COMPILER=clang++']

    if cxxflags: opt += [f'CMAKE_CXX_FLAGS="{cxxflags}"']
    if ldflags: opt += [
        f'CMAKE_EXE_LINKER_FLAGS="{ldflags}"',
        f'CMAKE_MODULE_LINKER_FLAGS="{ldflags}"',
        f'CMAKE_SHARED_LINKER_FLAGS="{ldflags}"',
        f'CMAKE_STATIC_LINKER_FLAGS="{ldflags}"'
    ]
    make = cmake_make_program(target)
    if make: opt.append(f'CMAKE_MAKE_PROGRAM="{make}"')

    if config.android:
        opt += [
            'BUILD_ANDROID=ON',
            'TARGET_ARCH=ANDROID',
            'CMAKE_SYSTEM_NAME=Android',
            f'ANDROID_ABI={config.android_arch}',
            'ANDROID_ARM_NEON=TRUE',
            f'ANDROID_NDK="{config.ndk_path}"',
            f'NDK_DIR="{config.ndk_path}"',
            'NDK_RELEASE=r16b',
            f'ANDROID_STL={config.android_ndk_stl}',
            f'ANDROID_NATIVE_API_LEVEL={config.android_api}',
            'ANDROID_TOOLCHAIN=clang',
            'CMAKE_BUILD_WITH_INSTALL_RPATH=ON',
        ]
        if target.cmake_ndk_toolchain:
            opt += [f'CMAKE_TOOLCHAIN_FILE="{target.cmake_ndk_toolchain}"']
    elif config.ios:
        opt += [
            'IOS_PLATFORM=OS',
            'CMAKE_SYSTEM_NAME=Darwin',
            'CMAKE_XCODE_EFFECTIVE_PLATFORMS=-iphoneos',
            'CMAKE_OSX_ARCHITECTURES=arm64',
            'CMAKE_OSX_SYSROOT="/Applications/Xcode.app/Contents/Developer/Platforms/iPhoneOS.platform/Developer/SDKs/iPhoneOS.sdk"'
        ]
        if target.cmake_ios_toolchain:
            opt += [f'CMAKE_TOOLCHAIN_FILE="{target.cmake_ios_toolchain}"']
    return opt


def cmake_inject_env(target):
    config:BuildConfig = target.config
    if config.android:
        make = cmake_make_program(target)
        if make: os.environ['CMAKE_MAKE_PROGRAM'] = make
        os.environ['ANDROID_HOME'] = config.android_sdk_path
        os.environ['ANDROID_NDK'] = config.ndk_path
        os.environ['ANDROID_ABI'] = config.android_arch
        os.environ['NDK_RELEASE'] = 'r16b'
        os.environ['ANDROID_STL'] = config.android_ndk_stl
        os.environ['ANDROID_NATIVE_API_LEVEL'] = config.android_api
        os.environ['ANDROID_TOOLCHAIN']        = 'clang'
    elif config.ios:
        os.environ['IPHONEOS_DEPLOYMENT_TARGET'] = config.ios_version
    elif config.macos:
        os.environ['MACOSX_DEPLOYMENT_TARGET'] = config.macos_version


def cmake_build_config(target):
    conf = f'--config {target.cmake_build_type}'
    if target.install_target: conf += f' --target {target.install_target}'
    return conf


def mp_flags(target):
    config:BuildConfig = target.config
    if not target.enable_multiprocess_build: return ''
    if config.windows:     return f'/maxcpucount:{config.jobs}'
    if target.enable_unix_make:   return f'-j {config.jobs}'
    if target.enable_ninja_build: return ''
    if config.ios:         return f'-jobs {config.jobs}'
    if config.macos:       return f'-jobs {config.jobs}'
    return f'-j {config.jobs}'


def cmake_buildsys_flags(target):
    config: BuildConfig = target.config
    def get_flags():
        mpf = mp_flags(target)
        if config.windows:     return f'/v:m {mpf} /nologo'
        if target.enable_unix_make:   return mpf
        if target.enable_ninja_build: return ''
        if config.android:     return mpf
        if config.ios:         return f'-quiet {mpf}'
        if config.macos:       return f'-quiet {mpf}'
        return mpf
    flags = get_flags()
    return f'-- {flags}' if flags else ''

