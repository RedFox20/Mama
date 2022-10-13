import os, subprocess, time
from mama.system import System, console, execute
from mama.build_config import BuildConfig
from mama.async_file_reader import AsyncConsoleReader


def _rerunnable_cmake_conf(cwd, args, allow_rerun, target):
    rerun = False
    error = ''
    delete_cmakecache = False
    print_enabled = target.config.print
    verbose = target.config.verbose
    if verbose: console(args)
    #xcode_filter = (target.ios or target.macos) and not target.enable_ninja_build 
    # TODO: use forktty instead of AsyncConsoleReader
    proc = subprocess.Popen(args, shell=True, universal_newlines=True, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    reader = AsyncConsoleReader(proc.stdout, proc.stderr)

    def handle_error(err):
        nonlocal rerun, delete_cmakecache
        print(err, flush=True, end='')
        if err.startswith('CMake Error: The source'):
            rerun = True
            delete_cmakecache = True
        elif System.windows:
            # this happens every time MSVC compiler is updated. simple fix is to rerun cmake
            rerun |= err.startswith('  is not a full path to an existing compiler tool.')
        elif err.startswith('CMake Error: Error: generator :') or \
             err.startswith('CMake Error: The source'):
            rerun = True
            delete_cmakecache = True

    def read_out_err():
        while reader.available():
            out, err = reader.read()
            if out:
                if print_enabled:
                    print(out, flush=True, end='')
            elif err:
                handle_error(err)

    while proc.poll() is None:
        read_out_err()
        time.sleep(0.015)
    reader.stop()
    read_out_err()
    
    if delete_cmakecache:
        if print_enabled: console('Deleting CMakeCache.txt')
        os.remove(target.build_dir('CMakeCache.txt'))
    if rerun and allow_rerun:
        if print_enabled: console('Rerunning CMake configure')
        return _rerunnable_cmake_conf(cwd, args, False, target)
    if proc.returncode != 0:
        raise Exception(f'CMake configure error: {error}')


def run_config(target):
    def get_flags():
        flags = ''
        options = target.cmake_opts + _default_options(target) + target.get_product_defines()
        for opt in options: flags += '-D'+opt+' '
        return flags
    
    cmake_flags = get_flags()
    generator = _generator(target)
    src_dir = target.source_dir()
    cmd = f'cmake {generator} {cmake_flags} -DCMAKE_INSTALL_PREFIX="." "{src_dir}"'
    _rerunnable_cmake_conf(target.build_dir(), cmd, True, target)


def run_build(target, install, extraflags=''):
    build_dir = target.build_dir()
    flags = _build_config(target, install)
    extraflags = _buildsys_flags(target)
    execute(f'cmake --build {build_dir} {flags} {extraflags}', echo=target.config.verbose)


def _generator(target):
    config:BuildConfig = target.config
    if target.enable_unix_make:   return '-G "Unix Makefiles"'
    if config.windows:            return f'-G "{config.get_visualstudio_cmake_id()}" -A {config.get_visualstudio_cmake_arch()}'
    if target.enable_ninja_build: return '-G "Ninja"'
    if config.android:            return '-G "Unix Makefiles"'
    if config.linux:              return '-G "Unix Makefiles"'
    if config.raspi:              return '-G "Unix Makefiles"'
    if config.oclea:              return '-G "Unix Makefiles"'
    if config.ios:                return '-G "Xcode"'
    if config.macos:              return '-G "Xcode"'
    else:                         return ''


def _make_program(target):
    config:BuildConfig = target.config
    if config.windows:     return ''
    if target.enable_unix_make:   return ''
    if target.enable_ninja_build: return config.ninja_path
    if config.android:
        if System.windows:
            return f'{config.android_ndk()}\\prebuilt\\windows-x86_64\\bin\\make.exe' # CodeBlocks - Unix Makefiles
        elif System.macos:
            return f'{config.android_ndk()}/prebuilt/darwin-x86_64/bin/make' # CodeBlocks - Unix Makefiles
    return ''


def _custom_compilers(target):
    config:BuildConfig = target.config
    cc, cxx = config.get_preferred_compiler_paths(target.enable_cxx_build)
    compilers = [f'CMAKE_C_COMPILER={cc}']
    if target.enable_cxx_build:
        compilers.append(f'CMAKE_CXX_COMPILER={cxx}')
    return compilers


def _default_options(target):
    config:BuildConfig = target.config
    cxxflags:dict = target.cmake_cxxflags
    ldflags:dict  = target.cmake_ldflags
    exceptions = target.enable_exceptions

    def add_flag(flag:str, value=''):
        if not flag in cxxflags:  # add flag if not already set
            cxxflags[flag] = value
    #def add_ldflag(flag:str, value=''): ldflags[flag] = value
    def get_flags_string(flags:dict):
        res = ''
        sep = ':' if config.windows else '='
        for k, v in flags.items():
            if not v:
                res += f' {k}'
            elif k.startswith('-D') and not '=' in k:
                res += f' {k}={v}'
            else:
                res += f' {k}{sep}{v}'
        return res.lstrip()
    
    if config.windows:
        add_flag('/EHsc')
        add_flag('-D_HAS_EXCEPTIONS', '1' if exceptions else '0')
        add_flag('-DWIN32', '1') # so yeah, only _WIN32 is defined by default, but opencv wants to see WIN32
        add_flag('/MP') # multi-process build
    else:
        if target.gcc_clang_visibility_hidden:
            add_flag('-fvisibility', 'hidden')
        if not exceptions: add_flag('-fno-exceptions')
    
    if config.android:
        if config.is_target_arch_armv7():
            add_flag('-march', 'armv7-a')
            add_flag('-mfpu', 'neon')
        else:
            add_flag('-march', 'armv8-a')
        if config.android_ndk_stl == 'c++_shared':
            add_flag(f'-I"{config.android_ndk()}/sources/cxx-stl/llvm-libc++/include"')
    elif config.linux:
        add_flag('-march', config.get_gcc_linux_march())
        if config.clang and target.enable_cxx_build:
            add_flag('-stdlib', 'libc++')
    elif config.macos:
        add_flag('-march', config.get_gcc_linux_march())
        if target.enable_cxx_build:
            add_flag('-stdlib', 'libc++')
    elif config.ios:
        add_flag('-arch arm64')
        add_flag('-miphoneos-version-min', config.ios_version)
        if target.enable_cxx_build:
            add_flag('-stdlib', 'libc++')
    elif config.raspi:
        add_flag('--sysroot', config.raspi_sysroot())
        for path in config.raspi_includes():
            add_flag(f'-I {path}')
    elif config.oclea:
        add_flag('--sysroot', config.oclea_sysroot())
        for path in config.oclea_includes():
            add_flag(f'-I {path}')

    if config.flags:
        add_flag(config.flags)

    opt = ["CMAKE_POSITION_INDEPENDENT_CODE=ON"]
    if config.linux or config.raspi or config.oclea:
        opt += _custom_compilers(target)
    
    if target.enable_fortran_build and config.fortran:
        opt += [f'CMAKE_Fortran_COMPILER={config.fortran}']

    cxxflags_str = get_flags_string(cxxflags)
    if cxxflags_str and target.enable_cxx_build:
        opt += [f'CMAKE_CXX_FLAGS="{cxxflags_str}"']

    ldflags_str = get_flags_string(ldflags)
    if ldflags_str: opt += [
        f'CMAKE_EXE_LINKER_FLAGS="{ldflags_str}"',
        f'CMAKE_MODULE_LINKER_FLAGS="{ldflags_str}"',
        f'CMAKE_SHARED_LINKER_FLAGS="{ldflags_str}"',
        f'CMAKE_STATIC_LINKER_FLAGS="{ldflags_str}"'
    ]

    make = _make_program(target)
    if make: opt.append(f'CMAKE_MAKE_PROGRAM="{make}"')

    if config.windows:
        if config.is_target_arch_x86(): ## need to override the toolset host
            opt.append('CMAKE_GENERATOR_TOOLSET=host=x86')
        if config.is_target_arch_x64():   opt.append('MAMA_ARCH_X64=TRUE')
        elif config.is_target_arch_x86(): opt.append('MAMA_ARCH_X86=TRUE')
    elif config.linux:
        if config.is_target_arch_x64():   opt.append('MAMA_ARCH_X64=TRUE')
        elif config.is_target_arch_x86(): opt.append('MAMA_ARCH_X86=TRUE')
    elif config.android:
        toolchain = target.cmake_ndk_toolchain
        if toolchain:
            toolchain = target.source_dir(toolchain)
        else:
            toolchain = f'{config.android_ndk()}/build/cmake/android.toolchain.cmake'
        opt += [
            'BUILD_ANDROID=ON',
            'TARGET_ARCH=ANDROID',
            'CMAKE_SYSTEM_NAME=Android',
            f'ANDROID_ABI={config.android_abi()}',
            'ANDROID_ARM_NEON=TRUE',
            f'ANDROID_NDK="{config.android_ndk()}"',
            f'NDK_DIR="{config.android_ndk()}"',
            f'NDK_RELEASE={config.android_ndk_release}',
            f'ANDROID_STL={config.android_ndk_stl}',
            f'ANDROID_NATIVE_API_LEVEL={config.android_api}',
            'ANDROID_TOOLCHAIN=clang',
            'CMAKE_BUILD_WITH_INSTALL_RPATH=ON',
            f'CMAKE_TOOLCHAIN_FILE="{toolchain}"'
        ]
    elif config.raspi:
        opt += [
            'RASPI=TRUE',
            'CMAKE_SYSTEM_NAME=Linux',
            'CMAKE_SYSTEM_VERSION=1',
            'CMAKE_SYSTEM_PROCESSOR=armv7-a', # ALWAYS ARMv7
            'CMAKE_FIND_ROOT_PATH_MODE_PROGRAM=NEVER', # Use our definitions for compiler tools
            'CMAKE_FIND_ROOT_PATH_MODE_LIBRARY=ONLY', # Search for libraries and headers in the target directories only
            'CMAKE_FIND_ROOT_PATH_MODE_INCLUDE=ONLY',
        ]
        if target.cmake_raspi_toolchain:
            toolchain = target.source_dir(target.cmake_raspi_toolchain)
            if config.print: console(f'Toolchain: {toolchain}')
            opt += [f'CMAKE_TOOLCHAIN_FILE="{toolchain}"']
    elif config.oclea:
        opt += [
            'OCLEA=TRUE',
            'CMAKE_SYSTEM_NAME=Linux',
            'CMAKE_SYSTEM_VERSION=1',
            'CMAKE_SYSTEM_PROCESSOR=arm64',
            'CMAKE_FIND_ROOT_PATH_MODE_PROGRAM=NEVER', # Use our definitions for compiler tools
            'CMAKE_FIND_ROOT_PATH_MODE_LIBRARY=ONLY', # Search for libraries and headers in the target directories only
            'CMAKE_FIND_ROOT_PATH_MODE_INCLUDE=ONLY',
        ]
        if target.cmake_oclea_toolchain:
            toolchain = target.source_dir(target.cmake_oclea_toolchain)
            if config.print: console(f'Toolchain: {toolchain}')
            opt += [f'CMAKE_TOOLCHAIN_FILE="{toolchain}"']
    elif config.macos:
        pass
    elif config.ios:
        opt += [
            'IOS_PLATFORM=OS',
            'CMAKE_SYSTEM_NAME=Darwin',
            'CMAKE_XCODE_EFFECTIVE_PLATFORMS=-iphoneos',
            'CMAKE_OSX_ARCHITECTURES=arm64', # ALWAYS ARM64
            #'CMAKE_OSX_SYSROOT=/Applications/Xcode.app/Contents/Developer/Platforms/iPhoneOS.platform/Developer/SDKs/iPhoneOS.sdk',
            'CMAKE_OSX_SYSROOT=iphoneos',
        ]
        if target.cmake_ios_toolchain:
            toolchain = target.source_dir(target.cmake_ios_toolchain)
            if config.print: console(f'Toolchain: {toolchain}')
            opt += [f'CMAKE_TOOLCHAIN_FILE="{toolchain}"']
    return opt


def inject_env(target):
    config:BuildConfig = target.config
    if config.android:
        make = _make_program(target)
        if make: os.environ['CMAKE_MAKE_PROGRAM'] = make
        os.environ['ANDROID_HOME'] = config.android_home()
        os.environ['ANDROID_NDK'] = config.android_ndk()
        os.environ['ANDROID_ABI'] = config.android_abi()
        os.environ['NDK_RELEASE'] = 'r16b'
        os.environ['ANDROID_STL'] = config.android_ndk_stl
        os.environ['ANDROID_NATIVE_API_LEVEL'] = config.android_api
        os.environ['ANDROID_TOOLCHAIN']        = 'clang'
    elif config.ios:
        os.environ['IPHONEOS_DEPLOYMENT_TARGET'] = config.ios_version
    elif config.macos:
        os.environ['MACOSX_DEPLOYMENT_TARGET'] = config.macos_version


def _build_config(target, install):
    conf = f'--config {target.cmake_build_type}'
    if install and target.install_target:
        conf += f' --target {target.install_target}'
    return conf


def _mp_flags(target):
    config:BuildConfig = target.config
    if not target.enable_multiprocess_build: return ''
    if config.windows:     return f'/maxcpucount:{config.jobs}'
    if target.enable_unix_make:   return f'-j{config.jobs}'
    if target.enable_ninja_build: return ''
    if config.ios:         return f'-jobs {config.jobs}'
    if config.macos:       return f'-jobs {config.jobs}'
    return f'-j{config.jobs}'


def _buildsys_flags(target):
    config:BuildConfig = target.config
    def get_flags():
        mpf = _mp_flags(target)
        if config.windows:            return f'/v:m {mpf} /nologo'
        if target.enable_unix_make:   return mpf
        if target.enable_ninja_build: return ''
        if config.android:            return mpf
        if config.ios or config.macos:
            if not target.config.verbose:
                return f'-quiet {mpf}'
        return mpf
    flags = get_flags()
    return f'-- {flags}' if flags else ''

