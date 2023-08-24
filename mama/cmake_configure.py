from __future__ import annotations
from typing import TYPE_CHECKING
import os
from .utils.system import System, console, Color
from .utils.sub_process import SubProcess, execute_piped_echo

if TYPE_CHECKING:
    from .build_target import BuildTarget
    from .build_config import BuildConfig


def _rerunnable_cmake_conf(cmd, cwd, allow_rerun, target:BuildTarget, delete_cmakecache:bool = False):
    rerun = False
    error = ''
    print_enabled = target.config.print
    verbose = target.config.verbose
    if verbose: console(cmd)
    #xcode_filter = (target.ios or target.macos) and not target.enable_ninja_build 

    if delete_cmakecache:
        if print_enabled: console('Deleting CMakeCache.txt')
        os.remove(target.build_dir('CMakeCache.txt'))

    def handle_output(line:str):
        nonlocal rerun, delete_cmakecache
        print(line) # newline is not included
        if line.startswith('CMake Error: The source'):
            rerun = True
            delete_cmakecache = True
        elif System.windows:
            # this happens every time MSVC compiler is updated. simple fix is to rerun cmake
            rerun |= line.startswith('  is not a full path to an existing compiler tool.')
        elif line.startswith('CMake Error: Error: generator :') or \
             line.startswith('CMake Error: The source'):
            rerun = True
            delete_cmakecache = True

    # run CMake configure and handle output
    exit_status = SubProcess.run(cmd, cwd, io_func=handle_output)

    if rerun and allow_rerun:
        if print_enabled: console('Rerunning CMake configure')
        return _rerunnable_cmake_conf(cmd, cwd, False, target, delete_cmakecache=delete_cmakecache)
    if exit_status != 0:
        raise Exception(f'CMake configure error: {error}')


def run_config(target:BuildTarget):
    if not target.config.update and os.path.exists(target.build_dir('CMakeCache.txt')):
        if target.config.verbose:
            console('Not running CMake configure because CMakeCache.txt exists and `update` was not specified')
        return

    def get_flags():
        flags = ''
        options = target.cmake_opts + _default_options(target) + target.get_product_defines()
        for opt in options: flags += '-D'+opt+' '
        return flags

    type_flags = f'-DCMAKE_BUILD_TYPE={target.cmake_build_type}'
    cmake_flags = get_flags()
    generator = _generator(target)
    src_dir = os.path.dirname(target.dep.cmakelists_path())
    src_dir = src_dir if src_dir else target.source_dir()
    install_prefix = '-DCMAKE_INSTALL_PREFIX="."'
    # # use install prefix override for libraries, but for root target, leave it open-ended
    # install_prefix = '' if target.dep.is_root else '-DCMAKE_INSTALL_PREFIX="."'
    cmd = f'cmake {generator} {type_flags} {cmake_flags} {install_prefix} "{src_dir}"'
    _rerunnable_cmake_conf(cmd, target.build_dir(), True, target)


def is_rerunnable_error(output:str):
    """ Checks output string if a rerunnable error occurred. 
        These are non-fatal errors that disappear with a simple cmake configure. """
    return 'Makefile: No such file or directory' in output


def run_build(target:BuildTarget, install:bool, extraflags='', rerun=True):
    build_dir = target.build_dir()
    flags = _build_config(target, install)
    extraflags = _buildsys_flags(target)
    cmd = f'cmake --build {build_dir} {flags} {extraflags}'
    if target.config.verbose:
        console(cmd, color=Color.GREEN)
    status, output = execute_piped_echo(build_dir, cmd, echo=True)
    if status != 0:
        if rerun and is_rerunnable_error(output):
            if target.config.verbose:
                console(f'Build {target.name} failed, attempting to rerun config', color=Color.GREEN)
            cmake_cache = target.build_dir('CMakeCache.txt')
            if os.path.exists(cmake_cache):
                os.remove(cmake_cache)
            run_config(target)
            run_build(target, install, extraflags, rerun=False)
        else:
            raise Exception(f'{cmd} failed with return code {status}')


def _generator(target:BuildTarget):
    config:BuildConfig = target.config
    if target.enable_unix_make:   return '-G "Unix Makefiles"'
    if config.windows:            return f'-G "{config.get_visualstudio_cmake_id()}" -A {config.get_visualstudio_cmake_arch()}'
    if target.enable_ninja_build: return '-G "Ninja"'
    if config.android:            return '-G "Unix Makefiles"'
    if config.linux:              return '-G "Unix Makefiles"'
    if config.raspi:              return '-G "Unix Makefiles"'
    if config.oclea:              return '-G "Unix Makefiles"'
    if config.mips:               return '-G "Unix Makefiles"'
    if config.ios:                return '-G "Xcode"'
    if config.macos:              return '-G "Xcode"'
    else:                         return ''


def _make_program(target:BuildTarget):
    config:BuildConfig = target.config
    if config.windows: return ''
    if target.enable_unix_make: return ''
    if target.enable_ninja_build: return config.ninja_path
    return ''


def _custom_compilers(target:BuildTarget):
    compilers = []
    cc, cxx, ver = target.config.get_preferred_compiler_paths()
    if cc:
        compilers.append(f'CMAKE_C_COMPILER={cc}')
        if target.enable_cxx_build:
            compilers.append(f'CMAKE_CXX_COMPILER={cxx}')
    return compilers


def _default_options(target:BuildTarget):
    config:BuildConfig = target.config
    cxxflags:dict = target.cmake_cxxflags
    ldflags:dict = target.cmake_ldflags
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
        if not exceptions:
            add_flag('-fno-exceptions')

    if config.android:
        config.android.get_cxx_flags(add_flag)
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
        config.oclea.get_cxx_flags(add_flag)
    elif config.mips:
        config.mips.get_cxx_flags(add_flag)

    if config.flags:
        add_flag(config.flags)

    opt = [
        "CMAKE_POSITION_INDEPENDENT_CODE=ON",
        "CMAKE_EXPORT_COMPILE_COMMANDS=ON" # for tools like clang-tidy and .vscode intellisense
    ]
    if config.linux or config.raspi or config.oclea or config.mips:
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
    elif config.android:
        opt += config.android.get_cmake_build_opts(target)
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
        opt += config.oclea.get_cmake_build_opts()
    elif config.mips:
        opt += config.mips.get_cmake_build_opts()
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


def inject_env(target:BuildTarget):
    config:BuildConfig = target.config
    if config.android:
        config.android.inject_env()
    elif config.ios:
        os.environ['IPHONEOS_DEPLOYMENT_TARGET'] = config.ios_version
    elif config.macos:
        os.environ['MACOSX_DEPLOYMENT_TARGET'] = config.macos_version


def _build_config(target:BuildTarget, install:bool):
    conf = f'--config {target.cmake_build_type}'
    if install and target.install_target:
        conf += f' --target {target.install_target}'
    return conf


def _mp_flags(target:BuildTarget):
    config:BuildConfig = target.config
    if not target.enable_multiprocess_build: return ''
    if config.windows:     return f'/maxcpucount:{config.jobs}'
    if target.enable_unix_make:   return f'-j{config.jobs}'
    if target.enable_ninja_build: return ''
    if config.ios:         return f'-jobs {config.jobs}'
    if config.macos:       return f'-jobs {config.jobs}'
    return f'-j{config.jobs}'


def _buildsys_flags(target:BuildTarget):
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

