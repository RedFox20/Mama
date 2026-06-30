"""Cross-build-dir reuse of CMake compiler detection (~5s of a ~6.5s cold configure, cut to ~1.7s).

Mechanism (a warm reconfigure of the same dir reproduced in a fresh dir, validated cross-project):
capture the toolchain detection files (`CMakeFiles/<ver>/CMake{C,CXX,RC}Compiler.cmake`,
`CMakeSystem.cmake`, the ABI `.bin`, VS `VCTargetsPath.txt`); inject them into a fresh build dir +
write a `CMakeCache.txt` with the `CMAKE_PLATFORM_INFO_INITIALIZED` marker (makes cmake trust the
cached info) + `CMAKE_HOME_DIRECTORY`. ONLY toolchain detection is transplanted, never project
flags - so a seed can't poison another project. The fingerprint auto-invalidates on toolchain
change; a failed seeded configure self-heals. Pure file/string ops + injected clock -> no-cmake tests."""

from __future__ import annotations
import os, shutil, hashlib, json, time, threading
from .util import path_join, normalized_path

# lang -> (compiler module file, ABI probe binary or None)
_LANG_FILES = {
    'C':   ('CMakeCCompiler.cmake',   'CMakeDetermineCompilerABI_C.bin'),
    'CXX': ('CMakeCXXCompiler.cmake', 'CMakeDetermineCompilerABI_CXX.bin'),
    'RC':  ('CMakeRCCompiler.cmake',  None),
}
_SHARED_FILES = ['CMakeSystem.cmake']
_VS_FILES = ['VCTargetsPath.txt']  # VS-generator MSBuild probe result (reusable, toolset-bound)
_MANIFEST = 'seed.json'
BACKSTOP_TTL = 7 * 24 * 3600  # seconds; fingerprint is the real gate, this is just paranoia


def compute_fingerprint(inputs: dict) -> str:
    """Stable 16-hex hash of every toolchain input that affects detection (cmake version, generator,
    compiler path+mtime+size, SDK, ...). A toolchain change flips the hash -> auto-invalidate."""
    blob = json.dumps(inputs, sort_keys=True, default=str)
    return hashlib.sha1(blob.encode('utf-8')).hexdigest()[:16]


def compiler_stat(path: str) -> dict:
    """Path + size + mtime of a compiler binary, for the fingerprint. {} if missing."""
    try:
        st = os.stat(path)
        return {'path': normalized_path(path), 'size': st.st_size, 'mtime': int(st.st_mtime)}
    except OSError:
        return {'path': path}


def detected_langs(build_files_dir: str) -> list:
    """Which languages a build dir actually detected (by which compiler files it wrote)."""
    return [lang for lang, (mod, _) in _LANG_FILES.items()
            if os.path.exists(path_join(build_files_dir, mod))]


def _seed_file_names(langs: list) -> list:
    names = list(_SHARED_FILES) + list(_VS_FILES)
    for lang in langs:
        mod, abi = _LANG_FILES[lang]
        names.append(mod)
        if abi: names.append(abi)
    return names


def publish(seed_dir: str, build_files_dir: str, fingerprint='', probe='', clock=time.time) -> bool:
    """Capture detection artifacts from a freshly-configured `build_files_dir`
    (`<build>/CMakeFiles/<ver>`) into `seed_dir`. Returns False if nothing usable was found. Each
    file lands via a temp + os.replace so a concurrent reader never copies a half-written file. The
    manifest records `fingerprint` (so a load can re-verify it) and `probe` (the compiler binary whose
    disappearance invalidates the seed)."""
    langs = detected_langs(build_files_dir)
    if not langs: return False
    os.makedirs(seed_dir, exist_ok=True)
    copied = []
    for name in _seed_file_names(langs):
        src = path_join(build_files_dir, name)
        if os.path.exists(src):
            dst = path_join(seed_dir, name)
            shutil.copy2(src, dst + '.tmp'); os.replace(dst + '.tmp', dst)  # atomic: no partial reads
            copied.append(name)
    manifest = {'created': int(clock()), 'cmake_files_ver': os.path.basename(build_files_dir.rstrip('/')),
                'langs': langs, 'files': copied, 'fingerprint': fingerprint, 'probe': probe}
    mtmp = path_join(seed_dir, _MANIFEST + '.tmp')
    with open(mtmp, 'w', encoding='utf-8') as f: json.dump(manifest, f)
    os.replace(mtmp, path_join(seed_dir, _MANIFEST))  # manifest last + atomic (load() gates on it)
    return True


def is_valid(manifest, fingerprint: str) -> bool:
    """Cheap recheck that a loaded seed still matches the live toolchain: its embedded fingerprint equals
    the current one AND the recorded compiler binary still exists. Pure stat/compare - never runs cmake."""
    if not manifest or manifest.get('fingerprint') != fingerprint:
        return False
    probe = manifest.get('probe')
    return not probe or os.path.exists(probe)


def gc_stale(seed_root: str, log=lambda m: None):
    """One cheap sweep of sibling seeds: drop any that can't be valid anymore - a legacy seed from an
    older mama (no fingerprint recorded) or one whose compiler binary is gone (an upgraded/removed
    toolset). A seed for a still-installed toolchain is untouched, so alternate-config seeds survive."""
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


def load(seed_dir: str, ttl=BACKSTOP_TTL, clock=time.time):
    """Return the manifest dict if a valid (present + not past the backstop TTL) seed exists, else None."""
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
    """Make a fresh `build_dir` look already-configured so cmake skips ALL detection: copy the
    captured toolchain files into CMakeFiles/<ver> + write a CMakeCache.txt with the
    PLATFORM_INFO_INITIALIZED marker + CMAKE_HOME_DIRECTORY. Returns False writing NO marker when the
    seed is empty or vanished mid-copy (a concurrent heal), so the caller detects normally instead of
    trusting a marker with no compiler files."""
    manifest = load(seed_dir, ttl=float('inf'))
    os.makedirs(build_files_dir, exist_ok=True)
    copied = 0
    for name in (manifest.get('files', []) if manifest else []):
        src = path_join(seed_dir, name)
        if not os.path.exists(src): continue
        try:
            shutil.copy2(src, path_join(build_files_dir, name)); copied += 1
        except OSError:
            return False  # seed file vanished under us (a concurrent purge): bail, don't trust it
    if not copied: return False
    cache = (f'CMAKE_PLATFORM_INFO_INITIALIZED:INTERNAL=1\n'
             f'CMAKE_HOME_DIRECTORY:INTERNAL={normalized_path(src_dir)}\n')
    with open(path_join(build_dir, 'CMakeCache.txt'), 'w', encoding='utf-8') as f:
        f.write(cache)
    return True


def purge(seed_dir: str):
    """Drop a seed (self-heal after a seeded configure fails). Never raises."""
    shutil.rmtree(seed_dir, ignore_errors=True)


class Coordinator:
    """Elects ONE configure job per fingerprint to pay detection + publish the seed; the rest block
    until it lands, then reuse it. In-process election only (cross-process races just redo detection -
    harmless, publish is idempotent). Injected `fp_fn(target)` + `paths_fn(target)` -> no-cmake tests."""

    def __init__(self, seed_root, fp_fn, paths_fn, probe_fn=None, enabled=True, clock=time.time,
                 wait_timeout=180.0, log_fn=None):
        self._root = seed_root
        self._fp = fp_fn
        self._paths = paths_fn
        self._probe = probe_fn or (lambda t: '')  # compiler binary recorded so a dead toolset self-invalidates
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
        """Once per mama session (not per package): log the seed root and sweep stale seeds. Called from
        the coordinator factory so it runs even when every build dir is already configured (prepare skipped)."""
        with self._lock:
            if self._gc_done: return
            self._gc_done = True  # flag set under lock first -> exactly one thread sweeps, the rest skip
        if not self._enabled:
            self._log('compiler-seed cache: disabled (nocache)'); return
        self._log(f'compiler-seed cache: {self._root}')
        gc_stale(self._root, self._log)

    def _try_use(self, target, fp) -> bool:
        """Load this fp's seed and, if it's still valid, inject it. Purges a present-but-stale seed so a
        clean one gets rebuilt (toolset moved, or a legacy seed with no fingerprint)."""
        sd = self.seed_dir(target)
        m = load(sd, clock=self._clock)
        if is_valid(m, fp) and inject(sd, *self._paths(target)):
            return True
        if m is not None: purge(sd)
        return False

    def prepare(self, target) -> str:
        """Decide and apply this target's role: 'use' (seed injected into the fresh build dir,
        cmake will skip detection), 'prime' (this caller publishes on success), or 'none'."""
        if not self._enabled: return 'none'
        self.begin_session()
        fp = self._fp(target)
        if self._try_use(target, fp):
            return 'use'
        with self._lock:
            if fp not in self._states:
                self._states[fp] = {'event': threading.Event(), 'ok': False}
                return 'prime'
            st = self._states[fp]
        st['event'].wait(self._wait)  # another job is priming - wait for it
        if st['ok'] and self._try_use(target, fp):
            return 'use'
        return 'none'  # primer failed/timed out, or the seed vanished: detect normally

    def publish(self, target):
        """Primer succeeded: capture its detection artifacts and wake the waiters. ALWAYS wakes them
        (even if the capture itself raises) so a publish error can't strand waiters for wait_timeout."""
        try:
            ok = publish(self.seed_dir(target), self._paths(target)[1],
                         fingerprint=self._fp(target), probe=self._probe(target), clock=self._clock)
        except Exception:
            ok = False
        self._finish(self._fp(target), ok)

    def fail_primer(self, target):
        """Primer's configure failed: wake waiters with no seed (they detect normally)."""
        self._finish(self._fp(target), False)

    def heal(self, target):
        """A seeded ('use') configure failed: drop the seed so the retry detects clean."""
        purge(self.seed_dir(target))

    def _finish(self, fp, ok):
        with self._lock:
            st = self._states.pop(fp, None)  # pop so a failed prime can be re-elected later
        if st:
            st['ok'] = ok
            st['event'].set()
