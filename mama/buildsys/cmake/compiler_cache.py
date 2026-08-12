"""Cross-build-dir reuse of CMake compiler detection, which cuts a cold configure from about 6.5s to 1.7s.
publish() captures only the toolchain detection files, never project flags, and inject() replays them."""

from __future__ import annotations
import os, shutil, hashlib, json, time, threading
from mama.utils.fileio import read_text_from
from mama.utils.paths import path_join, normalized_path

# lang -> (compiler module file, ABI probe binary or None)
_LANG_FILES = {
    'C':   ('CMakeCCompiler.cmake',   'CMakeDetermineCompilerABI_C.bin'),
    'CXX': ('CMakeCXXCompiler.cmake', 'CMakeDetermineCompilerABI_CXX.bin'),
    'RC':  ('CMakeRCCompiler.cmake',  None),
}
_SHARED_FILES = ['CMakeSystem.cmake']
_VS_FILES = ['VCTargetsPath.txt']  # VS-generator MSBuild probe result (reusable, toolset-bound)
_MANIFEST = 'seed.json'
# Cache entries the injected CMakeCache.txt must carry, replayed verbatim from the probe's own cache.
# The ABI probe writes its result to the CACHE only, and seeding skips the probe, so without the
# replay every install-RPATH executable fails. The compiler and toolchain entries must match the -D
# options mama passes, or cmake wipes the cache MID-CONFIGURE and re-detects a cross build as the host.
# clang-scan-deps is a find_program result of compiler detection, and a C++20 module target reads it.
# Without the replay the scan command runs "" and every module build fails with "Permission denied".
_REPLAY_CACHE_KEYS = ('CMAKE_EXECUTABLE_FORMAT', 'CMAKE_LIBRARY_ARCHITECTURE',
                      'CMAKE_C_COMPILER', 'CMAKE_CXX_COMPILER', 'CMAKE_TOOLCHAIN_FILE',
                      'CMAKE_CXX_COMPILER_CLANG_SCAN_DEPS')

# Bumped when the seed shape changes. is_valid rejects an older format, so the probe runs again.
_SEED_FORMAT = 4
BACKSTOP_TTL = 7 * 24 * 3600  # seconds. The fingerprint is the real gate. This TTL is only a backstop.


def compute_fingerprint(inputs: dict) -> str:
    """Stable 16-hex hash of every toolchain input that affects detection: cmake version, generator,
    compiler path + mtime + size, SDK. A toolchain change flips the hash and invalidates the seed."""
    blob = json.dumps(inputs, sort_keys=True, default=str)
    return hashlib.sha1(blob.encode('utf-8')).hexdigest()[:16]


def compiler_stat(path: str) -> dict:
    """Path, size and mtime of a compiler binary, for the fingerprint. Only the path when stat fails."""
    try:
        st = os.stat(path)
        return {'path': normalized_path(path), 'size': st.st_size, 'mtime': int(st.st_mtime)}
    except OSError:
        return {'path': path}


def detected_langs(build_files_dir: str) -> list:
    """The languages a build dir detected, judged by the compiler module files it wrote."""
    return [lang for lang, (mod, _) in _LANG_FILES.items()
            if os.path.exists(path_join(build_files_dir, mod))]


def detection_is_partial(build_files_dir: str) -> bool:
    """True when a compiler module exists but its ABI probe never finished (a killed configure).
    cmake trusts such a stage-1 module on the next run, so every target_compile_features() fails with
    'no known features'. RC has no ABI step, so the check skips it."""
    for lang, (mod, abi) in _LANG_FILES.items():
        if not abi: continue
        path = path_join(build_files_dir, mod)
        if not os.path.exists(path): continue
        try: text = read_text_from(path)
        except OSError: return True  # unreadable module: treat it as partial, a needless redetect is cheap
        if f'set(CMAKE_{lang}_ABI_COMPILED TRUE)' not in text: return True
    return False


_CORE_LANGS = ('C', 'CXX')  # RC is windows-only and has no ABI step, so it never gates a seed


def covers_core_langs(langs) -> bool:
    """Backstop check. The probe is a C+CXX project, so its seed covers both. A seed that lacks one
    would let a project that enables it skip detection and fail on 'CMAKE_<lang>_COMPILER not set'."""
    return all(lang in (langs or []) for lang in _CORE_LANGS)


def _seed_file_names(langs: list) -> list:
    names = list(_SHARED_FILES) + list(_VS_FILES)
    for lang in langs:
        mod, abi = _LANG_FILES[lang]
        names.append(mod)
        if abi: names.append(abi)
    return names


_COMPILER_SET = 'set(CMAKE_{}_COMPILER "'  # the set() line of a generated CMake<lang>Compiler.cmake


def compiler_from_module(build_files_dir: str, lang: str) -> str:
    """The compiler path the captured CMake<lang>Compiler.cmake records. Returns '' when absent."""
    try: text = read_text_from(path_join(build_files_dir, _LANG_FILES[lang][0]))
    except OSError: return ''
    prefix = _COMPILER_SET.format(lang)
    for line in text.splitlines():
        if line.startswith(prefix): return line[len(prefix):].split('"', 1)[0]
    return ''


def usable_compilers(build_files_dir: str) -> dict:
    """{lang: compiler path} for the core languages, or {} when any of them is unusable.
    cmake records an empty CMAKE_<lang>_COMPILER when detection fails, and every build dir seeded
    from that compiles with "". Read the value back and stat it, so a seed always names a compiler
    this machine has, or mama writes no seed at all."""
    compilers = {}
    for lang in _CORE_LANGS:
        compiler = compiler_from_module(build_files_dir, lang)
        if not compiler or not os.path.exists(compiler): return {}
        compilers[lang] = compiler
    return compilers


def read_replay_cache_lines(build_dir: str, compilers: dict = None) -> list:
    """_REPLAY_CACHE_KEYS lines verbatim from a configured dir's CMakeCache.txt, for inject() to replay.
    cmake caches only what it detected itself, so a toolchain file or a seeded dir leaves NO
    CMAKE_<lang>_COMPILER entry, and a project that resets the compiler from an unset env var then
    compiles with "". `compilers` fills the missing entries, so the cache always names the compiler."""
    try: text = read_text_from(path_join(build_dir, 'CMakeCache.txt'))
    except OSError: text = ''
    lines = [ln for ln in text.splitlines() if ln.split(':', 1)[0] in _REPLAY_CACHE_KEYS]
    cached = {ln.split(':', 1)[0] for ln in lines}
    for lang, compiler in (compilers or {}).items():
        key = f'CMAKE_{lang}_COMPILER'
        if key not in cached: lines.append(f'{key}:FILEPATH={compiler}')
    return lines


def publish(seed_dir: str, build_files_dir: str, fingerprint='', probe='', build_dir='', clock=time.time) -> bool:
    """Capture detection artifacts from a freshly configured build dir into `seed_dir`. Returns False
    when the detection is not usable. Each file lands via a temp + os.replace, so a concurrent
    reader never copies a half-written file.
    build_files_dir: the `<build>/CMakeFiles/<ver>` dir of a completed configure
    fingerprint: recorded so a load can re-verify the toolchain
    probe: the compiler binary whose disappearance invalidates the seed
    build_dir: supplies the CMakeCache.txt lines that inject() replays
    clock: time source, default is time.time
    """
    langs = detected_langs(build_files_dir)
    compilers = usable_compilers(build_files_dir)
    # never publish a half-detected toolchain or one naming a compiler this machine does not have
    if not covers_core_langs(langs) or detection_is_partial(build_files_dir) or not compilers: return False
    os.makedirs(seed_dir, exist_ok=True)
    copied = []
    for name in _seed_file_names(langs):
        src = path_join(build_files_dir, name)
        if os.path.exists(src):
            dst = path_join(seed_dir, name)
            shutil.copy2(src, dst + '.tmp'); os.replace(dst + '.tmp', dst)  # atomic: no partial reads
            copied.append(name)
    manifest = {'created': int(clock()), 'format': _SEED_FORMAT,
                'cmake_files_ver': os.path.basename(build_files_dir.rstrip('/')),
                'langs': langs, 'files': copied, 'fingerprint': fingerprint, 'probe': probe,
                'compilers': compilers,
                'cache_lines': read_replay_cache_lines(build_dir, compilers) if build_dir else []}
    mtmp = path_join(seed_dir, _MANIFEST + '.tmp')
    with open(mtmp, 'w', encoding='utf-8') as f: json.dump(manifest, f)
    os.replace(mtmp, path_join(seed_dir, _MANIFEST))  # manifest last + atomic (load() gates on it)
    return True


def _compilers_are_live(manifest) -> bool:
    """True when the seed names a compiler for every core language and each one is still on disk. The
    publish already checked this, so a False here means the toolset moved after mama wrote the seed."""
    compilers = manifest.get('compilers') or {}
    return all(compilers.get(lang) and os.path.exists(compilers[lang]) for lang in _CORE_LANGS)


def is_valid(manifest, fingerprint: str) -> bool:
    """Cheap recheck that a loaded seed still matches the live toolchain: its embedded fingerprint equals
    the current one AND every recorded compiler still exists. Pure stat/compare - never runs cmake."""
    if not manifest or manifest.get('fingerprint') != fingerprint:
        return False
    if manifest.get('format') != _SEED_FORMAT:
        return False  # older shape: replays too few cache lines, so re-probe instead of reusing it
    if not covers_core_langs(manifest.get('langs')):
        return False  # a seed missing C or CXX cannot serve a project that enables it
    if not _compilers_are_live(manifest):
        return False  # the toolset moved since the publish: detect again rather than seed a dead path
    probe = manifest.get('probe')
    return not probe or os.path.exists(probe)


def gc_stale(seed_root: str, log=lambda m: None):
    """One cheap sweep of sibling seeds: drop any that cannot be valid anymore, a legacy seed with no
    fingerprint or one whose compiler binary is gone. A seed for a still-installed toolchain survives."""
    try: names = os.listdir(seed_root)
    except OSError: return
    for name in names:
        sd = path_join(seed_root, name)
        m = load(sd, ttl=float('inf'))
        if m is None: continue  # no manifest (maybe a publish in flight) - leave it
        probe = m.get('probe')
        if 'fingerprint' not in m:
            log(f'  drop stale seed {name} (legacy: no fingerprint)'); purge(sd)
        elif probe and not os.path.exists(probe):
            log(f'  drop stale seed {name} (compiler gone: {probe})'); purge(sd)
        elif not _compilers_are_live(m):
            log(f'  drop stale seed {name} (no live compiler recorded)'); purge(sd)


def load(seed_dir: str, ttl=BACKSTOP_TTL, clock=time.time):
    """Return the manifest dict when the seed exists and is inside the backstop TTL, else None."""
    mpath = path_join(seed_dir, _MANIFEST)
    if not os.path.exists(mpath): return None
    try:
        with open(mpath, encoding='utf-8') as f: manifest = json.load(f)
    except (OSError, ValueError):
        return None
    if clock() - manifest.get('created', 0) > ttl:
        return None
    return manifest


def inject(seed_dir: str, build_dir: str, build_files_dir: str, src_dir: str) -> bool:
    """Make a fresh `build_dir` look already configured, so cmake skips ALL detection. Copies the
    captured toolchain files into CMakeFiles/<ver>, then writes a CMakeCache.txt with the
    PLATFORM_INFO_INITIALIZED marker and CMAKE_HOME_DIRECTORY. Returns False and writes NO marker
    when the seed is empty, names no live compiler, or vanished mid-copy. The caller then detects normally."""
    manifest = load(seed_dir, ttl=float('inf'))
    if manifest and not _compilers_are_live(manifest): return False
    os.makedirs(build_files_dir, exist_ok=True)
    copied = 0
    for name in (manifest.get('files', []) if manifest else []):
        src = path_join(seed_dir, name)
        if not os.path.exists(src): continue
        try:
            shutil.copy2(src, path_join(build_files_dir, name)); copied += 1
        except OSError:
            return False  # a concurrent purge removed the seed file: fall back to normal detection
    if not copied: return False
    cache = (f'CMAKE_PLATFORM_INFO_INITIALIZED:INTERNAL=1\n'
             f'CMAKE_HOME_DIRECTORY:INTERNAL={normalized_path(src_dir)}\n')
    # ABI facts plus the compiler entries whose absence resets the cache mid-configure, see _REPLAY_CACHE_KEYS
    cache += ''.join(f'{line}\n' for line in manifest.get('cache_lines', []))
    with open(path_join(build_dir, 'CMakeCache.txt'), 'w', encoding='utf-8') as f:
        f.write(cache)
    return True


def purge(seed_dir: str):
    """Drop a seed (self-heal after a seeded configure fails). Never raises."""
    shutil.rmtree(seed_dir, ignore_errors=True)


class Coordinator:
    """Builds ONE seed per fingerprint from a synthetic C+CXX probe, then injects it into every fresh
    build dir. The probe, not the first real target, makes it safe: the seed always covers both core
    languages. The election is in-process. A cross-process race only probes twice for identical content."""

    def __init__(self, seed_root, fp_fn, paths_fn, probe_fn=None, seed_fn=None, enabled=True, clock=time.time,
                 wait_timeout=180.0, log_fn=None):
        self._root = seed_root
        self._fp = fp_fn
        self._paths = paths_fn
        self._probe = probe_fn or (lambda t: '')  # compiler binary recorded so a dead toolset self-invalidates
        self._seed_fn = seed_fn  # context manager: (target) -> (build_dir, build_files_dir) of a C+CXX probe
        self._enabled = enabled
        self._clock = clock
        self._wait = wait_timeout
        self._log = log_fn or (lambda m: None)
        self._lock = threading.Lock()
        self._states: dict = {}  # fp -> {'event': Event, 'ok': bool}
        self._gc_done = False

    def seed_dir(self, target) -> str:
        return path_join(self._root, self._fp(target))

    def status(self, target) -> tuple:
        """(fingerprint, seed-present) for this target - verbose diagnostics, even when prepare is skipped."""
        fp = self._fp(target)
        return fp, os.path.exists(path_join(self._root, fp))

    def begin_session(self):
        """Once per mama session (not per package): log the seed root and sweep stale seeds. The coordinator
        factory calls this, so it runs even when every build dir is already configured (prepare skipped)."""
        with self._lock:
            if self._gc_done: return
            self._gc_done = True  # the lock guards the flag, so exactly one thread sweeps and the rest skip
        if not self._enabled:
            self._log('compiler-seed cache: disabled (nocache)'); return
        self._log(f'compiler-seed cache: {self._root}')
        gc_stale(self._root, self._log)

    def _try_use(self, target, fp) -> bool:
        """Load this fp's seed and, when it is still valid, inject it. Purges a present-but-stale seed so
        a clean one gets rebuilt (toolset moved, or a legacy seed with no fingerprint)."""
        sd = self.seed_dir(target)
        m = load(sd, clock=self._clock)
        if is_valid(m, fp) and inject(sd, *self._paths(target)):
            return True
        if m is not None: purge(sd)
        return False

    def prepare(self, target) -> str:
        """'use' (seed injected into the fresh build dir, cmake skips detection) or 'none' (detect
        normally). The first caller per fingerprint runs the probe. The rest wait for it, then inject."""
        if not self._enabled: return 'none'
        self.begin_session()
        fp = self._fp(target)
        if self._try_use(target, fp): return 'use'
        with self._lock:
            elected = fp not in self._states
            if elected: self._states[fp] = {'event': threading.Event(), 'ok': False}
            st = self._states[fp]
        if elected:
            ok = self._build_seed(target, fp)
            self._finish(fp, ok)
        else:
            st['event'].wait(self._wait)  # the probe is running - wait for it
            ok = st['ok']
        return 'use' if ok and self._try_use(target, fp) else 'none'

    def _build_seed(self, target, fp) -> bool:
        """Run the synthetic probe and publish its detection. Never raises: a probe failure just means
        everyone detects normally."""
        if self._seed_fn is None: return False
        try:
            with self._seed_fn(target) as paths:  # the probe's temp tree lives until publish has copied it
                if not paths: return False
                build_dir, build_files_dir = paths
                ok = publish(self.seed_dir(target), build_files_dir, fingerprint=fp,
                             probe=self._probe(target), build_dir=build_dir, clock=self._clock)
                # a refused publish means every build dir detects its own toolchain: slow but correct, so log it
                if not ok: self._log(f'compiler-seed probe rejected: {build_files_dir} named no usable compiler')
                return ok
        except Exception as e:
            self._log(f'compiler-seed probe failed: {e}')
            return False

    def heal(self, target):
        """A seeded ('use') configure failed: drop the seed so the retry detects clean."""
        purge(self.seed_dir(target))

    def _finish(self, fp, ok):
        with self._lock:
            st = self._states.pop(fp, None)  # pop so a failed probe can be retried later
        if st:
            st['ok'] = ok
            st['event'].set()
