"""Cross-build-dir reuse of CMake compiler/ABI detection.

Each package configures in its own build dir, so CMake re-runs the slow C/CXX compiler ID +
ABI try_compile from scratch every time even though the toolchain is identical. Measured on a
real project: ~5s of a ~5.4s cold configure is pure detection. This module captures the
detection artifacts from the first successful configure (the "seed") and injects them into every
later build dir, so cmake skips detection.

Validated mechanism: copy `CMakeFiles/<ver>/CMake{C,CXX,RC}Compiler.cmake`, `CMakeSystem.cmake`
and the `CMakeDetermineCompilerABI_*.bin` into a fresh build dir AND pass `-C seed.cmake` setting
`CMAKE_<LANG>_ABI_COMPILED`/`CMAKE_<LANG>_COMPILER_WORKS`. The files alone only skip the
working-compiler check; the cache vars are what skip the ABI try_compile. Build stays correct.

Pure file/string ops with injected `clock` so it unit-tests with no cmake. Fingerprinting,
primer election and run_config wiring live in the caller."""

from __future__ import annotations
import os, re, shutil, hashlib, json, time, threading
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
_SEED_CMAKE = 'seed.cmake'
BACKSTOP_TTL = 7 * 24 * 3600  # seconds; fingerprint is the real gate, this is just paranoia


def compute_fingerprint(inputs: dict) -> str:
    """Stable 16-hex hash of every toolchain input that changes detection output. Caller passes
    cmake version, generator+arch+toolset, compiler path+version+mtime+size, SDK, platform, langs.
    A toolchain change (e.g. compiler update -> new mtime/size) flips the hash -> auto-invalidate."""
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


# Only the compiler path is safe to pre-seed. Seeding COMPILE_FEATURES (or the standard vars)
# without the compiler module's full standards context makes cmake fatal-error in
# CMakeCommonCompilerMacros, and seeding COMPILER_ID/VERSION doesn't actually skip ID detection -
# so we leave those to cmake and only skip the expensive ABI try_compile + working-compiler check.
def _seed_vars(lang: str) -> list:
    return [f'CMAKE_{lang}_COMPILER']


def _read_compiler_vars(build_files_dir: str, lang: str) -> dict:
    """Copy the detected `set(VAR ...)` values verbatim out of a captured CMake<LANG>Compiler.cmake."""
    try:
        text = open(path_join(build_files_dir, _LANG_FILES[lang][0]), encoding='utf-8').read()
    except OSError:
        return {}
    out = {}
    for var in _seed_vars(lang):
        m = re.search(rf'^set\({var} (.+)\)\s*$', text, re.MULTILINE)
        if m: out[var] = m.group(1).strip()
    return out


def _seed_cmake_text(langs: list, lang_vars: dict) -> str:
    """Initial-cache that skips the slow ABI try_compile + working-compiler check: the seeded
    compiler path + ABI_COMPILED/WORKS tell cmake the compiler is good. Values are cmake's own
    detected output; the fingerprint keys on the compiler so they can't go stale silently, and a
    failed seeded configure self-heals."""
    lines = []
    for lang in langs:
        for var, val in lang_vars.get(lang, {}).items():
            lines.append(f'set({var} {val} CACHE INTERNAL "")')
        if lang != 'RC': lines.append(f'set(CMAKE_{lang}_ABI_COMPILED TRUE CACHE INTERNAL "")')
        lines.append(f'set(CMAKE_{lang}_COMPILER_WORKS {"1" if lang == "RC" else "TRUE"} CACHE INTERNAL "")')
    return '\n'.join(lines) + '\n'


def _seed_file_names(langs: list) -> list:
    names = list(_SHARED_FILES) + list(_VS_FILES)
    for lang in langs:
        mod, abi = _LANG_FILES[lang]
        names.append(mod)
        if abi: names.append(abi)
    return names


def publish(seed_dir: str, build_files_dir: str, clock=time.time) -> bool:
    """Capture detection artifacts from a freshly-configured `build_files_dir`
    (`<build>/CMakeFiles/<ver>`) into `seed_dir`. Returns False if nothing usable was found."""
    langs = detected_langs(build_files_dir)
    if not langs: return False
    os.makedirs(seed_dir, exist_ok=True)
    copied = []
    for name in _seed_file_names(langs):
        src = path_join(build_files_dir, name)
        if os.path.exists(src):
            shutil.copy2(src, path_join(seed_dir, name))
            copied.append(name)
    lang_vars = {lang: _read_compiler_vars(build_files_dir, lang) for lang in langs}
    with open(path_join(seed_dir, _SEED_CMAKE), 'w', encoding='utf-8') as f:
        f.write(_seed_cmake_text(langs, lang_vars))
    manifest = {'created': int(clock()), 'cmake_files_ver': os.path.basename(build_files_dir.rstrip('/')),
                'langs': langs, 'files': copied}
    with open(path_join(seed_dir, _MANIFEST), 'w', encoding='utf-8') as f:
        json.dump(manifest, f)
    return True


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


def inject(seed_dir: str, build_files_dir: str) -> str:
    """Copy the seed's detection files into a target build dir's `CMakeFiles/<ver>` and return the
    `-C <seed.cmake>` argument to add to the configure command. Caller guarantees a valid seed."""
    os.makedirs(build_files_dir, exist_ok=True)
    manifest = load(seed_dir, ttl=float('inf')) or {}
    for name in manifest.get('files', []):
        src = path_join(seed_dir, name)
        if os.path.exists(src):
            shutil.copy2(src, path_join(build_files_dir, name))
    return f'-C "{path_join(seed_dir, _SEED_CMAKE)}"'


def purge(seed_dir: str):
    """Drop a seed (self-heal after a seeded configure fails). Never raises."""
    shutil.rmtree(seed_dir, ignore_errors=True)


class Coordinator:
    """Elects ONE configure job per fingerprint to pay detection and publish the seed; the rest
    block until it lands, then reuse it. In-process election (the parallel scheduler's threads);
    cross-process races just cause redundant detection - harmless, since publish is idempotent and
    content-identical. `fp_fn(target)->str` and `bfd_fn(target)->CMakeFiles/<ver> path` are injected
    so this unit-tests with no cmake."""

    def __init__(self, seed_root, fp_fn, bfd_fn, enabled=True, clock=time.time, wait_timeout=180.0):
        self._root = seed_root
        self._fp = fp_fn
        self._bfd = bfd_fn
        self._enabled = enabled
        self._clock = clock
        self._wait = wait_timeout
        self._lock = threading.Lock()
        self._states: dict = {}  # fp -> {'event': Event, 'ok': bool}

    def seed_dir(self, target) -> str:
        return path_join(self._root, self._fp(target))

    def prepare(self, target):
        """Decide this target's role. Returns (extra_cmake_arg, role):
        'use' a published seed (arg = -C ...), 'prime' (this caller publishes on success), or
        'none' (configure normally)."""
        if not self._enabled: return ('', 'none')
        sd = self.seed_dir(target)
        if load(sd, clock=self._clock):
            return (inject(sd, self._bfd(target)), 'use')
        fp = self._fp(target)
        with self._lock:
            if fp not in self._states:
                self._states[fp] = {'event': threading.Event(), 'ok': False}
                return ('', 'prime')
            st = self._states[fp]
        st['event'].wait(self._wait)  # another job is priming - wait for it
        if st['ok'] and load(sd, clock=self._clock):
            return (inject(sd, self._bfd(target)), 'use')
        return ('', 'none')  # primer failed/timed out: detect normally

    def publish(self, target):
        """Primer succeeded: capture its detection artifacts and wake the waiters."""
        ok = publish(self.seed_dir(target), self._bfd(target), clock=self._clock)
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
