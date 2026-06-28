from __future__ import annotations
from typing import TYPE_CHECKING
import os, shutil, threading
from .utils.system import System, console, Color, warning
from .utils.sub_process import SubProcess, execute_piped_echo, execute_piped
from mama import util
from mama import cmake_compiler_cache as seedcache

if TYPE_CHECKING:
    from .build_target import BuildTarget
    from .build_config import BuildConfig


def _rerunnable_cmake_conf(cmd, cwd, allow_rerun, target:BuildTarget, delete_cmakecache:bool = False, env=None, out=None):
    rerun = False
    error = ''
    if target.config.verbose: console(cmd)

    if delete_cmakecache:
        if target.config.print: console('Deleting CMakeCache.txt')
        os.remove(target.build_dir('CMakeCache.txt'))

    def handle_output(p:SubProcess, line:str):
        nonlocal rerun, delete_cmakecache
        if out: out(line)
        else:   print(line) # newline is not included
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
    exit_status = SubProcess.run(cmd, cwd, env=env, io_func=handle_output)

    if rerun and allow_rerun:
        if target.config.print: console('Rerunning CMake configure')
        return _rerunnable_cmake_conf(cmd, cwd, False, target, delete_cmakecache=delete_cmakecache, env=env, out=out)
    if exit_status != 0:
        raise Exception(f'CMake configure error: {error}')
    target.dep.save_enabled_sanitizers()
    target.dep.save_enabled_coverage()


def _set_compiler_paths(target:BuildTarget, opt:list[str]):
    """
    Configures compilers for CMake, this needs to be done every time to prevent Ninja
    or other backends incorrectly picking wrong compilers. CC/CXX are stripped from the
    subprocess env by `compute_env` (not the global os.environ), so this is thread-safe.
    """
    cc, cxx, ver = target.config.get_preferred_compiler_paths()
    if cc:
        opt.append(f'CMAKE_C_COMPILER={util.forward_slashes(cc)}')
        if target.enable_cxx_build:
            opt.append(f'CMAKE_CXX_COMPILER={util.forward_slashes(cxx)}')
    elif 'CC' in os.environ or 'CXX' in os.environ:
        warning('Warning: CMake C/C++ compiler not detected and Global ENV CC/CXX are set')


def compute_env(target:BuildTarget) -> dict:
    """Per-job environment for a cmake invocation: a COPY of os.environ with CC/CXX removed
    when we pass explicit -DCMAKE_*_COMPILER (cmake prioritizes CC/CXX env otherwise). Returns
    a fresh dict so parallel configure/build jobs never mutate the shared global env."""
    env = os.environ.copy()
    cc, cxx, _ = target.config.get_preferred_compiler_paths()
    if cc:
        env.pop('CC', None)
        if target.enable_cxx_build: env.pop('CXX', None)
    return env


def _opts_to_defines(opts:list[str]) -> str:
    opts_defines = ''
    for opt in opts: opts_defines += '-D'+opt+' '
    return opts_defines


_seed_lock = threading.Lock()


def _cmake_version_number(config) -> str:
    """Parsed cmake version (e.g. '4.2.3'), which is also the CMakeFiles/<ver> dir name. Cached."""
    v = getattr(config, '_cmake_ver_num', None)
    if v is None:
        out = execute_piped([config.cmake_command, '--version'], throw=False) or ''
        nums = [ln.split()[-1] for ln in out.splitlines() if 'version' in ln.lower()]
        v = nums[0] if nums else 'unknown'
        config._cmake_ver_num = v
    return v


def _build_files_dir(target:BuildTarget) -> str:
    return util.path_join(target.build_dir(), f'CMakeFiles/{_cmake_version_number(target.config)}')


def _seed_src_dir(target:BuildTarget) -> str:
    """The dir cmake configures (the CMAKE_HOME_DIRECTORY the injected cache must match)."""
    d = os.path.dirname(target.dep.cmakelists_path())
    return d if d else target.source_dir()


def _seed_paths(target:BuildTarget):
    return (target.build_dir(), _build_files_dir(target), _seed_src_dir(target))


def _seed_inputs(target:BuildTarget) -> dict:
    config = target.config
    cc, cxx, ver = config.get_preferred_compiler_paths()
    return {
        'cmake': _cmake_version_number(config), 'gen': _generator(target),
        'arch': getattr(config, 'arch', ''), 'platform': config.platform_build_dir_name(),
        'cc': seedcache.compiler_stat(cc) if cc else {}, 'cxx': seedcache.compiler_stat(cxx) if cxx else {},
        'cver': ver, 'sdk': os.environ.get('WindowsSDKVersion', ''),
    }


def _seed_coordinator(target:BuildTarget) -> seedcache.Coordinator:
    """Lazily build the per-run, config-shared Coordinator. Seed lives in the workspace `packages`
    dir (dirname(dirname(build_dir))) so deleting `packages/` purges it."""
    config = target.config
    co = getattr(config, '_seed_coord', None)
    if co is not None: return co
    with _seed_lock:
        co = getattr(config, '_seed_coord', None)
        if co is None:
            root = util.path_join(os.path.dirname(os.path.dirname(target.build_dir())), '.mama_compiler_seed')
            co = seedcache.Coordinator(root, fp_fn=lambda t: seedcache.compute_fingerprint(_seed_inputs(t)),
                                       paths_fn=_seed_paths, enabled=not getattr(config, 'no_compiler_cache', False))
            config._seed_coord = co
        return co


def _wipe_build_dir(target:BuildTarget):
    """Drop CMakeCache + CMakeFiles so a self-heal retry detects cleanly."""
    cache = target.build_dir('CMakeCache.txt')
    if os.path.exists(cache): os.remove(cache)
    shutil.rmtree(util.path_join(target.build_dir(), 'CMakeFiles'), ignore_errors=True)


def run_config(target:BuildTarget, out=None, _seed=True):
    out = out if out is not None else getattr(target, '_out_sink', None)  # capture even custom build()s
    must_configure = target.config.update or target.config.run_cmake_configure
    # also reconfigure if sanitizer flags changed
    if not must_configure:
        current_sanitizers = target.config.sanitize or ''
        previous_sanitizers = target.dep.get_enabled_sanitizers()
        if current_sanitizers != previous_sanitizers:
            must_configure = True

    if not must_configure and os.path.exists(target.build_dir('CMakeCache.txt')):
        if target.config.verbose:
            console('Not running CMake configure because CMakeCache.txt exists and `update` or `configure` was not specified')
        return

    type_flags = f'-DCMAKE_BUILD_TYPE={target.cmake_build_type}'
    options = target.cmake_opts + _default_options(target) + target.get_product_defines()
    cmake_defines = _opts_to_defines(options)
    generator = _generator(target)
    src_dir = _seed_src_dir(target)
    install_prefix = '-DCMAKE_INSTALL_PREFIX="."'
    # # use install prefix override for libraries, but for root target, leave it open-ended
    # install_prefix = '' if target.dep.is_root else '-DCMAKE_INSTALL_PREFIX="."'

    # Reuse cached compiler detection on a fresh build dir: prepare() injects a CMakeFiles seed +
    # a PLATFORM_INFO_INITIALIZED CMakeCache so cmake skips ALL detection (~5s) (validated correct).
    cache_exists = os.path.exists(target.build_dir('CMakeCache.txt'))
    coord = _seed_coordinator(target)
    role = coord.prepare(target) if (_seed and not cache_exists) else 'none'

    cmd = f'{target.cmake_command} {generator} {type_flags} {cmake_defines} {install_prefix} "{src_dir}"'
    try:
        _rerunnable_cmake_conf(cmd, target.build_dir(), True, target, env=compute_env(target), out=out)
    except Exception:
        if role == 'prime':
            coord.fail_primer(target)
        elif role == 'use':  # a stale seed can only cost one extra detection: drop it, retry clean
            coord.heal(target)
            _wipe_build_dir(target)
            return run_config(target, out=out, _seed=False)
        raise
    if role == 'prime':
        coord.publish(target)


def is_rerunnable_error(output:str):
    """ Checks output string if a rerunnable error occurred.
        These are non-fatal errors that disappear with a simple cmake configure. """
    return 'Makefile: No such file or directory' in output


def run_build(target:BuildTarget, install:bool, extraflags='', rerun=True, out=None):
    out = out if out is not None else getattr(target, '_out_sink', None)  # capture even custom build()s
    build_dir = target.build_dir()
    flags = _build_config(target, install)
    extraflags = _buildsys_flags(target)
    cmd = f'{target.cmake_command} --build {build_dir} {flags} {extraflags}'
    if target.config.verbose:
        console(cmd, color=Color.GREEN)
    status, output = execute_piped_echo(build_dir, cmd, echo=True, env=compute_env(target), out=out)
    if status != 0:
        if rerun and is_rerunnable_error(output):
            if target.config.verbose:
                console(f'Build {target.name} failed, attempting to rerun config', color=Color.GREEN)
            cmake_cache = target.build_dir('CMakeCache.txt')
            if os.path.exists(cmake_cache):
                os.remove(cmake_cache)
            run_config(target, out=out)
            run_build(target, install, extraflags, rerun=False, out=out)
        else:
            raise Exception(f'{cmd} failed with return code {status}')


def _generator(target:BuildTarget):
    config:BuildConfig = target.config
    if target.enable_ninja_build: return '-G "Ninja"'
    if target.enable_unix_make:   return '-G "Unix Makefiles"'
    if config.msvc:               return f'-G "{config.get_visualstudio_cmake_id()}" -A {config.get_visualstudio_cmake_arch()}'
    if config.android:            return '-G "Unix Makefiles"'
    if config.linux:              return '-G "Unix Makefiles"'
    if config.yocto_linux:        return '-G "Unix Makefiles"'
    if config.raspi:              return '-G "Unix Makefiles"'
    if config.mips:               return '-G "Unix Makefiles"'
    if config.ios:                return '-G "Xcode"'
    if config.macos:              return '-G "Xcode"'
    else:                         return ''


def _make_program(target:BuildTarget):
    config:BuildConfig = target.config
    if target.enable_ninja_build: return config.ninja_path
    if config.msvc: return ''
    if target.enable_unix_make: return ''
    return ''


def _default_options(target:BuildTarget):
    config:BuildConfig = target.config
    cxxflags:dict = target.cmake_cxxflags
    ldflags:dict = target.cmake_ldflags
    exceptions = target.enable_exceptions

    def add_flag(flag:str, value=''):
        if not flag in cxxflags:  # add flag if not already set
            cxxflags[flag] = value
    def add_ldflag(flag:str, value=''):
        if not flag in ldflags:  # add flag if not already set
            ldflags[flag] = value
    def get_flags_string(flags:dict):
        res = ''
        sep = ':' if config.msvc else '='
        for k, v in flags.items():
            if not v:
                res += f' {k}'
            elif k.startswith('-D') and not '=' in k:
                res += f' {k}={v}'
            else:
                res += f' {k}{sep}{v}'
        return res.lstrip()

    if config.msvc:
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
    elif config.yocto_linux:
        config.yocto_linux.get_cxx_flags(add_flag)
    elif config.mips:
        config.mips.get_cxx_flags(add_flag)

    if config.flags:
        add_flag(config.flags)

    ld_sanitize = ''
    ld_coverage = ''

    if config.sanitize:
        if config.msvc:
            console(f'Enabling sanitizers: {config.sanitize}', color=Color.MAGENTA)
            ld_sanitize = f'/fsanitize={config.sanitize}'
        elif config.gcc or config.clang:
            console(f'Enabling sanitizers: {config.sanitize}', color=Color.MAGENTA)
            ld_sanitize = f'-fsanitize={config.sanitize}'
            add_flag('-fsanitize', config.sanitize)
            add_flag('-fno-sanitize-recover', config.sanitize) # fail the build on the first sanitizer error (UBSan recovers by default)
            add_flag('-fno-omit-frame-pointer')
            add_flag('-fPIE')
            add_ldflag('-pie') # -pie is a linker flag

    if config.coverage:
        if config.msvc:
            option = 'edge' if config.coverage == 'default' else config.coverage
            console(f'Enabling coverage: /fsanitize-coverage={option}', color=Color.MAGENTA)
            add_flag('/fsanitize-coverage', option)
        elif config.gcc or config.clang:
            console(f'Enabling coverage: (gcov+gcovr)', color=Color.MAGENTA)
            add_flag('--coverage')
            if config.gcc:
                add_flag('-fprofile-abs-path') # use absolute paths to always find coverage info
            ld_coverage='--coverage'

    opt = [
        "CMAKE_POSITION_INDEPENDENT_CODE=ON",
        "CMAKE_EXPORT_COMPILE_COMMANDS=ON" # for tools like clang-tidy and .vscode intellisense
    ]
    if config.with_tests or (config.test and config.target_matches(target.name)):
        opt += ["ENABLE_TESTS=ON", "BUILD_TESTS=ON"]

    if config.clang_tidy_path:
        console('Enabling clang-tidy static analysis during build', color=Color.MAGENTA)
        opt += [f'CMAKE_C_CLANG_TIDY="{config.clang_tidy_path}"',
                f'CMAKE_CXX_CLANG_TIDY="{config.clang_tidy_path}"']

    _set_compiler_paths(target, opt)

    if target.enable_fortran_build and config.fortran:
        opt += [f'CMAKE_Fortran_COMPILER={config.fortran}']

    cxxflags_str = get_flags_string(cxxflags)
    if cxxflags_str and target.enable_cxx_build:
        opt += [f'CMAKE_CXX_FLAGS="{cxxflags_str}"']

    if config.yocto_linux:
        config.yocto_linux.get_ldflags_with_defaults(ldflags)

    ldflags_str = get_flags_string(ldflags)
    if ldflags_str:
        exe_ldflags = ldflags_str
        if ld_sanitize: exe_ldflags += ' ' + ld_sanitize
        if ld_coverage: exe_ldflags += ' ' + ld_coverage
        opt += [
            f'CMAKE_EXE_LINKER_FLAGS="{exe_ldflags}"',
            f'CMAKE_MODULE_LINKER_FLAGS="{exe_ldflags}"',
            f'CMAKE_SHARED_LINKER_FLAGS="{exe_ldflags}"',
            # NOTE: CMAKE_STATIC_LINKER_FLAGS is intentionally omitted because
            # it is passed to the archiver (ar), not the linker (ld),
            # and ar does not understand linker flags like -Wl,--as-needed
        ]

    make = _make_program(target)
    if make: opt.append(f'CMAKE_MAKE_PROGRAM="{make}"')

    if config.msvc:
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
    elif config.yocto_linux:
        opt += config.yocto_linux.get_cmake_build_opts()
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


def _jobs(target:BuildTarget) -> int:
    """Build parallelism for this target: a scheduler-sized `_build_jobs` (from the TU probe)
    when set, else the global `config.jobs`. Per-target so concurrent builds never clobber
    a shared `-j` value."""
    return getattr(target, '_build_jobs', None) or target.config.jobs


def _mp_flags(target:BuildTarget):
    config:BuildConfig = target.config
    if not target.enable_multiprocess_build: return ''
    jobs = _jobs(target)
    if config.msvc:       return f'/maxcpucount:{jobs}'
    if target.enable_unix_make:   return f'-j{jobs}'
    if config.ios:         return f'-jobs {jobs}'
    if config.macos:       return f'-jobs {jobs}'
    return f'-j{jobs}'


def _buildsys_flags(target:BuildTarget):
    if target.enable_ninja_build: return '' # ninja does not need extra flags
    config:BuildConfig = target.config
    def get_flags():
        mpf = _mp_flags(target)
        if config.msvc:               return f'/v:m {mpf} /nologo'
        if target.enable_unix_make:   return mpf
        if config.android:            return mpf
        if config.ios or config.macos:
            if not target.config.verbose:
                return f'-quiet {mpf}'
        return mpf
    flags = get_flags()
    return f'-- {flags}' if flags else ''

