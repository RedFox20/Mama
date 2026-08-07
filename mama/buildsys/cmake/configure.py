from __future__ import annotations
from typing import TYPE_CHECKING
import os, contextlib, re, shutil, tempfile, threading
from mama.utils.system import System, console, Color, warning, warning_to
from mama.utils.sub_process import SubProcess, execute_piped_echo, execute_piped
from mama.utils.errors import BuildError
from mama.utils.fileio import file_sha1, read_text_from, write_text_to
from mama.utils.paths import forward_slashes, normalized_path, path_join, user_cache_dir, workspace_mama_dir
from mama import build_names
from mama.buildsys.cmake import compiler_cache as seedcache
from mama.buildsys.cmake.options import platform_opts as _platform_opts

if TYPE_CHECKING:
    from mama.build_target import BuildTarget
    from mama.build_config import BuildConfig


def _rerunnable_cmake_conf(cmd, cwd, allow_rerun, target:BuildTarget, delete_cmakecache:bool = False, env=None, out=None):
    rerun = False
    if target.config.verbose: console(cmd)

    if delete_cmakecache:
        if target.config.print: console('Deleting CMakeCache.txt')
        os.remove(target.build_dir('CMakeCache.txt'))

    def handle_output(p:SubProcess, line:str):
        nonlocal rerun, delete_cmakecache
        if out: out(line)
        else:   console(line)  # NOT print: a raw write tears the live region's cursor math
        if line.startswith('CMake Error: The source'):
            rerun = True
            delete_cmakecache = True
        elif System.windows:
            # an MSVC compiler update triggers this every time, and a cmake rerun fixes it
            rerun |= line.startswith('  is not a full path to an existing compiler tool.')
        elif line.startswith('CMake Error: Error: generator :') or \
             line.startswith('CMake Error: The source'):
            rerun = True
            delete_cmakecache = True

    exit_status = SubProcess.run(cmd, cwd, env=env, io_func=handle_output)

    if rerun and allow_rerun:
        if target.config.print: console('Rerunning CMake configure')
        return _rerunnable_cmake_conf(cmd, cwd, False, target, delete_cmakecache=delete_cmakecache, env=env, out=out)
    if exit_status != 0:
        # BuildError, not Exception: the cmake output above already names the failure, so mama
        # reports a clean one-liner instead of a traceback through its internals.
        raise BuildError(f'CMake configure failed for {target.name} (exit code {exit_status})')
    target.dep.save_enabled_sanitizers()
    target.dep.save_enabled_coverage()


def _set_compiler_paths(target:BuildTarget, opt:list[str]):
    """Name the compilers for cmake on every configure, so no backend picks the wrong ones.
    `compute_env` strips CC/CXX from the subprocess env, so this is thread-safe. A toolchain file
    picks the compiler itself, so mama must not name one too - see use_toolchain_file."""
    if target.config.cmake_toolchain_file: return
    cc, cxx, ver = target.config.get_preferred_compiler_paths()
    if cc:
        opt.append(f'CMAKE_C_COMPILER={forward_slashes(cc)}')
        if target.enable_cxx_build:
            opt.append(f'CMAKE_CXX_COMPILER={forward_slashes(cxx)}')
    elif 'CC' in os.environ or 'CXX' in os.environ:
        warning('Warning: CMake C/C++ compiler not detected and Global ENV CC/CXX are set')


def compute_env(target:BuildTarget) -> dict:
    """Per-job cmake env: a COPY of os.environ with CC/CXX removed when we pass explicit
    -DCMAKE_*_COMPILER (cmake prioritizes CC/CXX otherwise). Fresh dict -> thread-safe."""
    env = os.environ.copy()
    cc, cxx, _ = target.config.get_preferred_compiler_paths()
    if cc:
        env.pop('CC', None)
        if target.enable_cxx_build: env.pop('CXX', None)
    return env


# a run of backslash-separated path segments, e.g. `\windows\bin\protoc.exe`. Deliberately NOT a bare
# backslash: an escaped quote (\") or a literal separator define (-DSEP=\\) must survive untouched.
_BACKSLASH_PATH = re.compile(r'(?:\\[\w.\-+~$()]+)+')


def _opts_to_defines(opts:list[str]) -> str:
    """`-D` flags for the cmake command line. Backslash PATHS become forward slashes: SubProcess
    shlex-splits the command, which silently strips them. cmake takes / on every platform."""
    opts_defines = ''
    for opt in opts:
        opts_defines += '-D' + _BACKSLASH_PATH.sub(lambda m: m.group(0).replace('\\', '/'), opt) + ' '
    return opts_defines


_seed_lock = threading.Lock()


def _cmake_version_number(config) -> str:
    """Parsed cmake version (e.g. '4.2.3'), which is also the CMakeFiles/<ver> dir name. Cached."""
    v = config._cmake_ver_num
    if v is None:
        out = execute_piped([config.cmake_command, '--version'], throw=False) or ''
        nums = [ln.split()[-1] for ln in out.splitlines() if 'version' in ln.lower()]
        v = nums[0] if nums else 'unknown'
        config._cmake_ver_num = v
    return v


def _build_files_dir(target:BuildTarget) -> str:
    return path_join(target.build_dir(), f'CMakeFiles/{_cmake_version_number(target.config)}')


def _seed_src_dir(target:BuildTarget) -> str:
    """The dir cmake configures (the CMAKE_HOME_DIRECTORY the injected cache must match)."""
    d = os.path.dirname(target.dep.cmakelists_path())
    return d if d else target.source_dir()


def _seed_paths(target:BuildTarget):
    return (target.build_dir(), _build_files_dir(target), _seed_src_dir(target))


_TOOLCHAIN_KEYS = ('CMAKE_TOOLCHAIN_FILE', 'CMAKE_SYSTEM_NAME', 'CMAKE_SYSTEM_PROCESSOR',
                   'CMAKE_OSX_SYSROOT', 'CMAKE_OSX_ARCHITECTURES', 'CMAKE_C_COMPILER', 'CMAKE_CXX_COMPILER')


def _toolchain_inputs(target:BuildTarget) -> dict:
    """Cross-compile inputs that change compiler detection but the cc/cxx stat does not catch. Built from
    the whole platform opt set, because an SDK move changes CMAKE_SYSROOT, not the 7 obvious keys. Empty
    dict for a native build."""
    pairs = [o.partition('=') for o in _platform_opts(target)]  # one split per option, not two
    out = {k: v for k, _, v in pairs}
    out.update({k: v for k, _, v in (o.partition('=') for o in target.cmake_opts) if k in _TOOLCHAIN_KEYS})
    # _platform_opts recorded the effective toolchain file. Stat it so an in-place edit also invalidates.
    tc = target.config.cmake_toolchain_file
    if tc: out['CMAKE_TOOLCHAIN_FILE'] = seedcache.compiler_stat(tc)
    return out


def _seed_probe(target:BuildTarget) -> str:
    """The compiler binary whose disappearance means the seed is stale. MSVC leaves the compiler
    paths empty, so resolve the toolset's cl.exe explicitly. seedcache records it and GC stats it cheaply."""
    config = target.config
    if config.msvc:
        try: return normalized_path(config.get_msvc_cl64())
        except Exception: return ''
    _, cxx, _ = config.get_preferred_compiler_paths()
    return normalized_path(cxx) if cxx else ''


def _seed_id(target:BuildTarget) -> str:
    """Platform-qualified seed id, e.g. `android-arm64-3f9c...`. A HOST seed reaching a cross build
    dir would make cmake skip system determination and compile with host flags, silently. The name
    carries the platform, so a hash collision cannot cause that, and the platform is obvious on disk."""
    config = target.config
    fp = seedcache.compute_fingerprint(_seed_inputs(target))
    # Config-only, NOT dep.build_dir_name: this names a COMPILER seed, and a dep's args do not
    # change compiler detection. Per-dep naming here would re-probe the compiler per arg set.
    return f'{build_names.build_dir_name(config)}-{config.arch}-{fp}'


def _seed_inputs(target:BuildTarget) -> dict:
    config = target.config
    cc, cxx, ver = config.get_preferred_compiler_paths()
    inputs = {
        'cmake': _cmake_version_number(config), 'gen': _generator(target),
        'arch': config.arch, 'platform': build_names.build_dir_name(config),
        'cc': seedcache.compiler_stat(cc) if cc else {},
        'cxx': seedcache.compiler_stat(cxx) if cxx else {},
        'cver': ver, 'sdk': os.environ.get('WindowsSDKVersion', ''),
        'toolchain': _toolchain_inputs(target),
        'stdlib': _abi_stdlib(config),  # libc++ vs libstdc++ changes the CXX ABI probe's implicit link libs
        # The seed shape belongs to the toolchain identity of a build dir. A new format flips every
        # fingerprint once, so run_config wipes the dirs an older seed shaped and seeds them again.
        'seedfmt': seedcache._SEED_FORMAT,
    }
    if config.msvc:  # MSVC leaves cc/cxx empty, so stat cl.exe directly - else a toolset upgrade is invisible
        inputs['msvc'] = seedcache.compiler_stat(_seed_probe(target))
    elif not cc:  # no explicit compiler -> CC/CXX env selects it, so they belong in the fingerprint
        inputs['env_cc'] = os.environ.get('CC', ''); inputs['env_cxx'] = os.environ.get('CXX', '')
    return inputs


def _abi_stdlib(config) -> str:
    """The -stdlib that reaches the CXX ABI probe. '' where there is no choice (only linux clang picks one)."""
    return config.clang_stdlib if (config.linux and config.clang) else ''


def _abi_flags(config) -> tuple:
    """(C, CXX) flags that change what the ABI probe records as implicit link libs, so the probe must
    detect with them. A sanitizer pulls its runtime into both. -stdlib is C++-only (clang warns on C)."""
    san = [f'-fsanitize={config.sanitize}'] if (config.sanitize and not config.msvc) else []
    stdlib = [f'-stdlib={_abi_stdlib(config)}'] if _abi_stdlib(config) else []
    return ' '.join(san), ' '.join(san + stdlib)


_SEED_PROJECT = 'cmake_minimum_required(VERSION 3.15)\nproject(mama_seed C CXX)\n'


@contextlib.contextmanager
def _probe_toolchain(target:BuildTarget):
    """Detect the toolchain in a throwaway C+CXX project, not in whichever real target configures
    first: a C-only one would seed no CXX. Yields (build_dir, build_files_dir), or None when the
    probe missed a language. The temp tree lives exactly until publish has copied it."""
    config = target.config
    c_abi, cxx_abi = _abi_flags(config)  # same ABI inputs as the real targets, per language
    flags = (f' -DCMAKE_C_FLAGS="{c_abi}"' if c_abi else '') + (f' -DCMAKE_CXX_FLAGS="{cxx_abi}"' if cxx_abi else '')
    # platform opts FIRST: they carry the sysroot + cross binutils (without them the probe detects the
    # HOST), and they record whether a toolchain file owns the compiler, which _set_compiler_paths reads
    opts = _platform_opts(target)
    _set_compiler_paths(target, opts)
    with tempfile.TemporaryDirectory(prefix='mama_seed_', ignore_cleanup_errors=True) as tmp:
        tmp = normalized_path(tmp)  # shlex eats backslashes: never interpolate a raw Windows path
        src, bld = path_join(tmp, 'src'), path_join(tmp, 'b')
        os.makedirs(src, exist_ok=True)
        write_text_to(path_join(src, 'CMakeLists.txt'), _SEED_PROJECT)
        cmd = f'{target.cmake_command} {_generator(target)} {_opts_to_defines(opts)}{flags} -S "{src}" -B "{bld}"'
        if config.verbose: console(f'  seed probe: {cmd}', color=Color.BLUE)
        if SubProcess.run(cmd, tmp, env=compute_env(target), io_func=lambda p, line: None) != 0:
            yield None; return
        files_dir = path_join(bld, f'CMakeFiles/{_cmake_version_number(config)}')
        yield (bld, files_dir) if seedcache.covers_core_langs(seedcache.detected_langs(files_dir)) else None


def _seed_root(target:BuildTarget) -> str:
    """Where the compiler seeds live.

    The workspace by default, under `packages/.mama/`, so `rm -rf packages/` still heals a broken seed.
    Under `globalcache` the root moves to the user cache dir. The seed id carries the platform, the arch
    and a compiler hash, so one probe there serves every checkout on this machine. A new checkout then
    skips the 4-second probe, which is what a CI job and the test suite want. A developer keeps the local
    root, because one bad seed in the user cache would reach every project."""
    if target.config.global_compiler_cache:
        return user_cache_dir('compiler_seed')
    return workspace_mama_dir(os.path.dirname(os.path.dirname(target.build_dir())), 'compiler_seed')


def _seed_coordinator(target:BuildTarget) -> seedcache.Coordinator:
    """Lazily build the per-run, config-shared Coordinator. See _seed_root for where the seeds live."""
    config = target.config
    co = config._seed_coord
    if co is not None: return co
    with _seed_lock:
        co = config._seed_coord
        if co is None:
            root = _seed_root(target)
            log = (lambda m: console(m, color=Color.BLUE)) if config.verbose else None
            co = seedcache.Coordinator(root, fp_fn=_seed_id,
                                       paths_fn=_seed_paths, probe_fn=_seed_probe, seed_fn=_probe_toolchain,
                                       log_fn=log,
                                       enabled=not config.no_compiler_cache)
            co.begin_session()  # once per session: log root + sweep stale seeds (even if every dir is configured)
            config._seed_coord = co
        return co


def _note(target:BuildTarget, out, text:str):
    """Report a build-dir decision on the target's OWN output, so the log keeps the reason next to
    the configure it explains. A bare warning() depends on the thread capture of the running phase."""
    if target.config.print: warning_to(out, f'  - Target {target.name: <16} {text}')


def _wipe_build_dir(target:BuildTarget):
    """Drop CMakeCache + CMakeFiles so a self-heal retry detects cleanly."""
    cache = target.build_dir('CMakeCache.txt')
    if os.path.exists(cache): os.remove(cache)
    shutil.rmtree(path_join(target.build_dir(), 'CMakeFiles'), ignore_errors=True)


def _cache_entry(cache_text:str, key:str) -> str:
    """Value of a `KEY:TYPE=value` line in a CMakeCache ('' if absent). Anchored to the exact key with an
    optional `:TYPE`, so CMAKE_GENERATOR skips its CMAKE_GENERATOR_PLATFORM/_TOOLSET/_INSTANCE siblings."""
    m = re.search(rf'^{re.escape(key)}(?::[^=\n]*)?=(.*)$', cache_text, re.MULTILINE)
    return m.group(1).strip() if m else ''


def cache_generator(cache_text:str) -> str:
    """The CMAKE_GENERATOR recorded in a CMakeCache ('Ninja', 'Unix Makefiles', ...), '' if absent."""
    return _cache_entry(cache_text, 'CMAKE_GENERATOR')


def generator_build_file_exists(build_dir:str, generator:str) -> bool:
    """True when the build file THIS generator emits exists. A leftover Makefile from another
    generator must NOT make a Ninja-configured dir look complete, or `cmake --build` dies on a
    missing build.ninja. An unrecognized generator is trusted rather than wrongly wiped."""
    gen = generator.lower()
    if 'ninja' in gen:         return os.path.exists(path_join(build_dir, 'build.ninja'))
    if 'makefiles' in gen:     return os.path.exists(path_join(build_dir, 'Makefile'))
    # VS 18 (2026) with cmake 4.2 writes the XML solution `.slnx`, every older toolset writes `.sln`
    if 'visual studio' in gen: return any(f.endswith(('.sln', '.slnx')) for f in os.listdir(build_dir))
    if 'xcode' in gen:         return any(f.endswith('.xcodeproj') for f in os.listdir(build_dir))
    return True


def is_cmake_cache_valid(build_dir:str) -> bool:
    """True only when `build_dir` holds the artifacts of a configure that ran to COMPLETION. A plain existence
    check misses three poisoned shapes: truncated cache (no CMAKE_GENERATOR), no generated build file, and
    the other generator's stale leftover file. All three -> reconfigure."""
    cache = path_join(build_dir, 'CMakeCache.txt')
    if not os.path.exists(cache): return False
    try: generator = cache_generator(read_text_from(cache))
    except OSError: return False  # an unreadable cache counts as missing -> reconfigure
    if not generator: return False
    return generator_build_file_exists(build_dir, generator)


def _sink(target, out):
    return out if out is not None else target._out_sink  # capture even custom build()s


_TOOLCHAIN_FINGERPRINT_FILE = 'mama_toolchain.fingerprint'


def _toolchain_fingerprint(target:BuildTarget) -> str:
    """Hash of THIS target's toolchain identity, the same inputs the seed cache fingerprints. Every
    completed configure records it, so the NEXT one can tell that the toolchain moved since."""
    return seedcache.compute_fingerprint(_seed_inputs(target))


def _read_toolchain_fingerprint(build_dir:str) -> str:
    """Fingerprint the last completed configure of `build_dir` recorded. '' if none (never configured, or a
    dir from before mama wrote fingerprints)."""
    try: return read_text_from(path_join(build_dir, _TOOLCHAIN_FINGERPRINT_FILE)).strip()
    except OSError: return ''


def _record_toolchain_fingerprint(build_dir:str, fingerprint:str):
    """Persist the toolchain fingerprint next to the cache. Best-effort: a write failure makes the next
    run treat the dir as unfingerprinted (adopt), never an exception mid-configure."""
    try: write_text_to(path_join(build_dir, _TOOLCHAIN_FINGERPRINT_FILE), fingerprint)
    except OSError: pass


_CONFIGURE_FINGERPRINT_FILE = 'mama_configure.fingerprint'


def _dependency_exports(target:BuildTarget) -> str:
    """Hash of `mama-dependencies.cmake`, the file that names every include dir and lib the dependencies
    of this target export. A dependency that rebuilds without changing its interface leaves this file
    alone, and its consumer then needs no configure. A new export lib or a moved include dir changes it."""
    exports = path_join(target.build_dir(), 'mama-dependencies.cmake')
    try: return file_sha1(exports)
    except OSError: return ''


def _configure_fingerprint(target:BuildTarget, toolchain:str, cmd_inputs:list) -> str:
    """Hash of EVERYTHING mama feeds one cmake configure: the toolchain, every option it passes, and the
    exports of its dependencies. An input this misses is a silently stale build, so `cmd_inputs` takes
    the whole option list, never a subset of it.

    A change to CMakeLists.txt is deliberately absent. That file belongs to cmake, which re-runs itself
    through its own ZERO_CHECK rule when any listed input is newer than the generated build system."""
    return seedcache.compute_fingerprint({'toolchain': toolchain, 'cmd': cmd_inputs,
                                          'exports': _dependency_exports(target)})


def _read_configure_fingerprint(build_dir:str) -> str:
    """What the last completed configure of `build_dir` recorded. '' when there is none."""
    try: return read_text_from(path_join(build_dir, _CONFIGURE_FINGERPRINT_FILE)).strip()
    except OSError: return ''


def _record_configure_fingerprint(build_dir:str, fingerprint:str):
    """Persist the configure fingerprint. Best-effort, like the toolchain one: a write failure only
    makes the next run configure again."""
    try: write_text_to(path_join(build_dir, _CONFIGURE_FINGERPRINT_FILE), fingerprint)
    except OSError: pass


def _toolchain_moved_unfingerprinted(build_dir:str, target:BuildTarget) -> bool:
    """One-time heal for a dir that predates recorded fingerprints. True only when the cached compiler is
    DEFINITELY not the current one. Two proofs: the recorded path differs from the preferred compiler, or
    the recorded binary left the disk. MSVC names no compiler, so only the second proof applies there. A
    toolset upgrade deletes the old directory, which is exactly that case. Never wipe on missing evidence,
    so this cannot mass-invalidate warm dirs."""
    if target.config.cmake_toolchain_file:
        return False  # the cache holds the toolchain's own choice, which never equals ours
    try: cache_text = read_text_from(path_join(build_dir, 'CMakeCache.txt'))
    except OSError: return False
    cc_path, cxx_path, _ = target.config.get_preferred_compiler_paths()
    for key, want in (('CMAKE_CXX_COMPILER', cxx_path), ('CMAKE_C_COMPILER', cc_path)):
        cached = _cache_entry(cache_text, key)
        if not cached: continue
        if want and normalized_path(cached) != normalized_path(want): return True  # compiler path moved
        # A relative name came from PATH, and os.path.exists cannot test it. Only an absolute path is proof.
        return os.path.isabs(cached) and not os.path.exists(cached)
    return False  # no compiler recorded in the cache -> nothing to compare, do not wipe


_MULTI_CONFIG_GENERATORS = ('visual studio', 'xcode', 'multi-config')


def is_multi_config(generator:str) -> bool:
    """True when the generator carries several configurations and picks one at build time. Accepts a
    cache entry (`Xcode`) or the command line flag (`-G "Xcode"`)."""
    gen = generator.lower()
    return any(g in gen for g in _MULTI_CONFIG_GENERATORS)


def cached_build_type(build_dir:str, single_config_only=False) -> str:
    """CMAKE_BUILD_TYPE recorded in a build dir, '' when the dir holds no cache.
    single_config_only: answer '' for a multi-config generator, which picks the type at build time,
                        so its cache does not say what the artifacts in the dir are."""
    try: cache = read_text_from(path_join(build_dir, 'CMakeCache.txt'))
    except OSError: return ''
    if single_config_only and is_multi_config(cache_generator(cache)):
        return ''
    return _cache_entry(cache, 'CMAKE_BUILD_TYPE')


def run_config(target:BuildTarget, out=None, _seed=True):
    out = _sink(target, out)
    must_configure = target.config.update or target.config.run_cmake_configure
    # also reconfigure if sanitizer flags changed
    if not must_configure:
        current_sanitizers = target.config.sanitize or ''
        previous_sanitizers = target.dep.get_enabled_sanitizers()
        if current_sanitizers != previous_sanitizers:
            must_configure = True

    # Debug and release share one build dir, so only the cache says which one it holds. A single-config
    # generator bakes the type in, so without this `mama build debug` silently rebuilds release.
    if not must_configure:
        recorded = cached_build_type(target.build_dir())
        if recorded and recorded != target.cmake_build_type:
            _note(target, out, f'build type changed {recorded} -> {target.cmake_build_type}, reconfiguring')
            must_configure = True

    # Wipe, never soft-reconfigure, a build dir whose toolchain has since MOVED: cmake keeps stale
    # cache vars like CMAKE_SYSTEM_PROCESSOR, which mis-drive the project's own CMakeLists. A differing
    # recorded fingerprint proves the move. A dir that predates fingerprints falls back to its cached compiler path.
    toolchain_fingerprint = _toolchain_fingerprint(target)
    if os.path.exists(target.build_dir('CMakeCache.txt')):
        recorded = _read_toolchain_fingerprint(target.build_dir())
        moved = recorded != toolchain_fingerprint if recorded \
                else _toolchain_moved_unfingerprinted(target.build_dir(), target)
        if moved:
            _note(target, out, 'toolchain changed since last configure - wiping build dir')
            _wipe_build_dir(target)

    # A half-written cache or compiler detection from a killed configure poisons this run, so drop both
    # and reconfigure clean. The detection check runs even with no cache at all: a kill mid-detection
    # often saves none, and a `use` seed would re-add the marker.
    if seedcache.detection_is_partial(_build_files_dir(target)) \
       or (os.path.exists(target.build_dir('CMakeCache.txt')) and not is_cmake_cache_valid(target.build_dir())):
        _note(target, out, 'incomplete build dir (interrupted configure) - rebuilding it')
        _wipe_build_dir(target)
    elif not must_configure and os.path.exists(target.build_dir('CMakeCache.txt')):
        if target.config.verbose:
            console('Not running CMake configure because CMakeCache.txt exists and `update` or `configure` was not specified')
        _record_toolchain_fingerprint(target.build_dir(), toolchain_fingerprint)  # adopt/refresh the baseline
        return

    type_flags = _type_flags(target)
    options = target.cmake_opts + _default_options(target) + target.get_product_defines()
    cmake_defines = _opts_to_defines(options)
    generator = _generator(target)
    src_dir = _seed_src_dir(target)
    # Last, so cmake_opts can never override it by accident. Set target.cmake_install_prefix instead.
    install_prefix = f'-DCMAKE_INSTALL_PREFIX="{target.cmake_install_prefix}"'

    # `update` asks for a configure per target, whether or not anything reached cmake differently. A warm
    # configure of a real project costs about 50 seconds, so compare the inputs first and skip when they
    # match. `mama configure` is the explicit override and never lands here.
    configure_fingerprint = _configure_fingerprint(target, toolchain_fingerprint,
                                                   [type_flags, cmake_defines, install_prefix, src_dir])
    if must_configure and not target.config.run_cmake_configure \
       and _read_configure_fingerprint(target.build_dir()) == configure_fingerprint \
       and is_cmake_cache_valid(target.build_dir()):
        _note(target, out, 'configure inputs unchanged - skipping cmake configure')
        return

    # Reuse cached compiler detection on a fresh build dir: prepare() injects a CMakeFiles seed and
    # a PLATFORM_INFO_INITIALIZED CMakeCache, so cmake skips ALL detection (about 5s).
    cache_exists = os.path.exists(target.build_dir('CMakeCache.txt'))
    coord = _seed_coordinator(target)
    role = coord.prepare(target) if (_seed and not cache_exists) else 'none'
    if target.config.verbose and _seed:
        fp, present = coord.status(target)
        outcome = role if not cache_exists else 'skip (CMakeCache exists)'
        console(f'  seed[{target.name}] fp={fp} {"hit" if present else "miss"} -> {outcome}', color=Color.BLUE)

    cmd = f'{target.cmake_command} {_unused_cli_flag(target)}{generator} {type_flags} {cmake_defines}' \
          f' {install_prefix} "{src_dir}"'
    try:
        _rerunnable_cmake_conf(cmd, target.build_dir(), True, target, env=compute_env(target), out=out)
    except Exception:
        if role == 'use':  # a stale seed can only cost one extra detection: drop it, retry clean
            coord.heal(target)
            _wipe_build_dir(target)
            return run_config(target, out=out, _seed=False)
        raise
    # Record both identities only after a completed configure, so the next run never compares against a
    # false baseline. A failed configure leaves the previous fingerprints, or none at all.
    _record_toolchain_fingerprint(target.build_dir(), toolchain_fingerprint)
    _record_configure_fingerprint(target.build_dir(), configure_fingerprint)


_RERUNNABLE_ERRORS = (
    'Makefile: No such file or directory',                # configure died before emitting the makefile
    "loading 'build.ninja': No such file or directory",   # ...or the ninja file (same, Ninja generator)
    'CMAKE_GENERATOR in Cache',                           # cache truncated by a killed configure
)


def is_rerunnable_error(output:str):
    """True when the output names a non-fatal error that a cmake configure rerun fixes."""
    return any(s in output for s in _RERUNNABLE_ERRORS)


def run_build(target:BuildTarget, install:bool, extraflags='', rerun=True, out=None):
    out = _sink(target, out)
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
            _wipe_build_dir(target)  # cache AND CMakeFiles: a partial cache leaves stale detection behind
            run_config(target, out=out)
            run_build(target, install, extraflags, rerun=False, out=out)
        else:
            raise BuildError(f'Build failed for {target.name} (exit code {status})')


def _unused_cli_flag(target:BuildTarget) -> str:
    """Silence cmake's unused-variables block: mama always passes CMAKE_C_COMPILER, so every C++-only
    project reports it. Kept under `verbose`, the only signal that an add_cmake_options() name is misspelled."""
    return '' if target.config.verbose else '--no-warn-unused-cli '


# The cmake generator per platform build system. MSVC is not here: its generator carries the
# detected Visual Studio version and the target arch, so it is built from config.
_GENERATORS = {'make': '-G "Unix Makefiles"', 'xcode': '-G "Xcode"'}


def _generator(target:BuildTarget):
    config:BuildConfig = target.config
    if target.enable_ninja_build: return '-G "Ninja"'
    if target.enable_unix_make:   return '-G "Unix Makefiles"'
    if config.msvc: return f'-G "{config.platform.generator_name()}" -A {config.platform.generator_arch()}'
    return _GENERATORS.get(config.platform.build_system, '')


def _type_flags(target:BuildTarget) -> str:
    """The build type on the cmake command line. A multi-config generator ignores CMAKE_BUILD_TYPE at
    build time, so it also gets the configurations it may offer, the type of this target first.
    The cmake default set holds two more that mama never builds, and a build of one cannot link."""
    active = target.cmake_build_type
    flags = f'-DCMAKE_BUILD_TYPE={active}'
    if is_multi_config(_generator(target)):
        other = 'RelWithDebInfo' if active == 'Debug' else 'Debug'
        flags += f' -DCMAKE_CONFIGURATION_TYPES="{active};{other}"'
    return flags


def _make_program(target:BuildTarget) -> str:
    """The build tool cmake drives: Ninja when the target enabled it, else what the platform
    provides. ONLY this place names it, or cmake gets CMAKE_MAKE_PROGRAM twice."""
    config:BuildConfig = target.config
    if target.enable_ninja_build: return config.ninja_path
    return config.platform.make_program(target)


def _default_options(target:BuildTarget):
    config:BuildConfig = target.config
    cxxflags:dict = target.cmake_cxxflags
    ldflags:dict = target.cmake_ldflags
    exceptions = target.enable_exceptions

    def add_flag(flag:str, value=''):
        if not flag in cxxflags:
            cxxflags[flag] = value
    def add_ldflag(flag:str, value=''):
        if not flag in ldflags:
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
        add_flag('-DWIN32', '1') # MSVC only defines _WIN32 by default, but opencv wants WIN32
        add_flag('/MP') # multi-process build
    else:
        if target.gcc_clang_visibility_hidden:
            add_flag('-fvisibility', 'hidden')
        if not exceptions:
            add_flag('-fno-exceptions')

    if config.buildstats and config.clang:  # instrument for the Linux/Clang buildstats deep dive
        add_flag('-ftime-trace')   # per-TU Chrome-trace JSON written beside each .o (GCC has no equivalent)

    config.platform.get_cxx_flags(add_flag)
    if target.enable_cxx_build:
        stdlib = config.platform.cxx_stdlib()  # only linux clang, macos and ios pick one
        if stdlib: add_flag('-stdlib', stdlib)

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
            add_flag('-fno-sanitize-recover', config.sanitize) # fail on the first sanitizer error (UBSan recovers by default)
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

    opt += _platform_opts(target)  # before _set_compiler_paths: it records the toolchain-file flag
    _set_compiler_paths(target, opt)

    if target.enable_fortran_build and config.fortran:
        opt += [f'CMAKE_Fortran_COMPILER={config.fortran}']

    cxxflags_str = get_flags_string(cxxflags)
    if cxxflags_str and target.enable_cxx_build:
        opt += [f'CMAKE_CXX_FLAGS="{cxxflags_str}"']

    config.platform.get_ld_flags(add_ldflag)

    ldflags_str = get_flags_string(ldflags)
    if ldflags_str:
        exe_ldflags = ldflags_str
        if ld_sanitize: exe_ldflags += ' ' + ld_sanitize
        if ld_coverage: exe_ldflags += ' ' + ld_coverage
        opt += [
            f'CMAKE_EXE_LINKER_FLAGS="{exe_ldflags}"',
            f'CMAKE_MODULE_LINKER_FLAGS="{exe_ldflags}"',
            f'CMAKE_SHARED_LINKER_FLAGS="{exe_ldflags}"',
            # CMAKE_STATIC_LINKER_FLAGS is omitted on purpose: cmake passes it to the archiver (ar),
            # not the linker (ld), and ar does not understand linker flags like -Wl,--as-needed
        ]

    make = _make_program(target)
    if make: opt.append(f'CMAKE_MAKE_PROGRAM="{make}"')
    return opt


def inject_env(target:BuildTarget):
    """Environment the build tools read. The platform sets its own SDK variables. The make program
    is cmake's own variable, so it is set here and not by the platform that supplies the path."""
    config:BuildConfig = target.config
    make = config.platform.make_program()
    if make: os.environ['CMAKE_MAKE_PROGRAM'] = make
    config.platform.inject_env()


def _build_config(target:BuildTarget, install:bool):
    conf = f'--config {target.cmake_build_type}'
    if install and target.install_target:
        conf += f' --target {target.install_target}'
    return conf


def _jobs(target:BuildTarget) -> int:
    """Build parallelism for this target: a scheduler-sized `_build_jobs` when set, else the global
    `config.jobs`. Per-target, so concurrent builds never clobber a shared `-j` value. The root
    runs alone after all deps, so it always gets full `config.jobs`."""
    if target.dep.is_root: return target.config.jobs
    return target._build_jobs or target.config.jobs


def _mp_flags(target:BuildTarget):
    config:BuildConfig = target.config
    if not target.enable_multiprocess_build: return ''
    jobs = _jobs(target)
    if config.msvc: return f'/maxcpucount:{jobs}'
    # a target that forced Unix Makefiles takes make's flag, whatever the platform prefers
    if target.enable_unix_make: return f'-j{jobs}'
    return f'-jobs {jobs}' if config.platform.build_system == 'xcode' else f'-j{jobs}'


def _buildsys_flags(target:BuildTarget):
    if target.enable_ninja_build: return '' # ninja does not need extra flags
    config:BuildConfig = target.config
    mpf = _mp_flags(target)
    if config.msvc:
        flags = f'/v:m {mpf} /nologo'
    elif config.platform.build_system == 'xcode' and not (target.enable_unix_make or config.verbose):
        flags = f'-quiet {mpf}'  # xcodebuild floods the output unless -quiet is set
    else:
        flags = mpf
    return f'-- {flags}' if flags else ''

