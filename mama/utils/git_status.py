"""Does a dependency's working tree differ from what the last build used?

Three layers, cheapest first. `source_fingerprint` walks the tree and reads only the directory entry,
`git status` answers authoritatively, and the run's shared status answers a local dependency with no
process at all. A git process costs about 23ms on Windows before git does any work, so the layer that
avoids one is worth its code."""

import os, subprocess, hashlib, time

from .system import System, Color, console
from .paths import path_join, forward_slashes, _NON_SOURCE_ENTRIES
from .fileio import read_text_from, write_text_to

_git_fingerprints = {}  # src_dir -> fingerprint, the memo of git_dir_fingerprint for one mama run
# During a build, mama is the only writer of a dependency tree, and run_git drops the entry whenever a
# subcommand changes one. A test that edits a tree itself sets this False, so every call asks git again.
memoize_git_fingerprints = True

_repo_status = None  # (repo root, {rel path: status code}) from ONE `git status` for the whole run

# `verbose` turns this on, see BuildConfig. Every working-tree check then says which dependency it read,
# which caller wanted it, where the answer came from, and what it found.
log_status_checks = False


# Directories that hold no build input. Tool and output dirs ONLY. `third_party`, `external` and
# `vendor` are deliberately absent, because real sources live in them in many projects.
_SKIP_DIRS = {
    '.git', '.svn', '.hg', '.jj',                                       # version control metadata
    'build', 'builds', '_build', 'out', 'bin', 'obj',                   # build output
    'cmakefiles', 'x64', 'x86', 'win32', 'debug', 'release',
    '.vs', '.vscode', '.idea', '.fleet', '.cache', '.ccache',           # editor and compiler caches
    'vcpkg', 'vcpkg_installed', '_deps', 'node_modules',                # package managers
    'bower_components', '.conan', '.conan2', '.cargo', '.gradle',
    '__pycache__', '.venv', 'venv', '.tox', '.mypy_cache', '.pytest_cache', '.ruff_cache',
    'packages',                                                         # the mama workspace dir
}
_SKIP_PREFIX = ('cmake-build-',)  # the CLion family: cmake-build-debug, cmake-build-release and so on

# What a C or C++ build reads. A README, a yaml or a LICENSE cannot change what the compiler produces,
# and `git status` rebuilds on all three today.
_SRC_EXTS = {'.c', '.cc', '.cpp', '.cxx', '.c++', '.cu', '.m', '.mm',
             '.h', '.hh', '.hpp', '.hxx', '.inl', '.ipp', '.ixx', '.tpp',
             '.s', '.asm', '.rc', '.def', '.cmake', '.in', '.proto', '.f', '.f90'}
_SRC_NAMES = {'cmakelists.txt', 'makefile', 'meson.build', 'mamafile.py'}


def is_build_input(name: str) -> bool:
    """True when a file of this name can change what the build produces."""
    return os.path.splitext(name)[1].lower() in _SRC_EXTS or os.path.basename(name).lower() in _SRC_NAMES


def source_fingerprint(src_dir: str) -> str:
    """Hash of (relative path, size, mtime) for every build input under `src_dir`, or '' when there is
    none. This is the CHEAP layer of the source check, and it runs on Windows only.

    os.scandir hands back the size and mtime from the directory entry there, so the walk opens nothing
    and stats nothing extra. It cost 88ms across 23 real dependencies where `git status` cost 933ms, one
    process each. On Linux `git status` is already that fast, so this layer does not run and cannot
    regress it.

    An mtime is not content, so a caller treats a difference as `ask git`, never as `rebuild`."""
    h, count = hashlib.sha1(), 0
    stack = [(src_dir, '')]
    while stack:
        path, rel = stack.pop()
        try: entries = sorted(os.scandir(path), key=lambda e: e.name)
        except OSError: continue  # a dir that vanished mid-walk cannot hold a build input either
        for entry in entries:
            name = entry.name.lower()
            try:
                if entry.is_dir(follow_symlinks=False):
                    if name not in _SKIP_DIRS and not name.startswith(_SKIP_PREFIX):
                        stack.append((entry.path, f'{rel}{entry.name}/'))
                elif is_build_input(name):
                    st = entry.stat()
                    h.update(f'{rel}{entry.name}\0{st.st_size}\0{st.st_mtime_ns}\0'.encode('utf-8', 'replace'))
                    count += 1
            except OSError:
                pass
    return f'{h.hexdigest()[:16]}-{count}' if count else ''


def git_source_changed(src_dir: str) -> bool:
    """True when git says a BUILD INPUT under `src_dir` differs from HEAD. The slow, authoritative layer.

    The filter is the point. `git status` answers 'does the tree differ from HEAD', so mama rebuilt a
    target when a developer edited a README, a yaml or a license. This asks the question mama means, and
    it runs on every platform, because that defect is not specific to one."""
    entries = _parse_status(_git_output(['status', '--porcelain', '-z', '--', '.'], src_dir))
    return any(is_build_input(path) for path in entries)


def source_walk_file(build_dir: str) -> str:
    """Where the last build recorded its source walk, beside git_status in the same build dir."""
    return path_join(build_dir, 'source_walk')


def source_walk_moved(src_dir: str, build_dir: str) -> bool:
    """True when the cheap walk sees a build input that moved since the last build recorded one.

    This is a GATE, not an answer. It runs on Windows only, in front of the git check, because a git
    process costs about 26ms there against 4ms for the walk. A normal build has nothing to report, so
    the gate ends the question. An mtime is not content, so a True here only means `ask git`. Off
    Windows it always returns True, and the git check answers alone."""
    if not System.windows: return True
    stored = ''
    try: stored = read_text_from(source_walk_file(build_dir)).strip()
    except OSError: pass
    if not stored: return True  # never recorded, so nothing to compare against
    return source_fingerprint(src_dir) != stored


def record_source_walk(src_dir: str, build_dir: str):
    """Record the walk this build used, so the next one can skip the git check. Windows only, and
    best-effort: a write failure only makes the next build ask git."""
    if not System.windows: return
    try: write_text_to(source_walk_file(build_dir), source_fingerprint(src_dir))
    except OSError: pass


def _log_status_check(src_dir: str, reason: str, source: str, found: str, seconds: float):
    """One line per working-tree check, so a slow run names the dependency and the caller that paid."""
    name = os.path.basename(src_dir.rstrip('/\\')) or src_dir
    console(f'  {name: <16} status [{reason}] {source} -> {found} ({1000*seconds:.0f}ms)', color=Color.BLUE)


def _kinds_text(kinds: tuple) -> str:
    """What a fresh check found, naming both kinds, because only untracked files skip the diff."""
    return ' '.join(w for w, on in zip(('tracked', 'untracked'), kinds) if on) or 'clean'


def _git_output(args: list, cwd: str) -> bytes:
    """The stdout of one git command, or b'' when it fails.

    Uses subprocess.run with stderr=DEVNULL, not SubProcess.run. A source dir may not be under git at
    all, and the `fatal: not a git repository` noise must not reach the user."""
    try:
        cp = subprocess.run(['git', *args], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, cwd=cwd, timeout=10)
        return cp.stdout if cp.returncode == 0 else b''
    except Exception:
        return b''


def _parse_status(out: bytes) -> dict:
    """{path: status code} for one `git status --porcelain -z`. A rename or a copy stores its old path
    in the next field, and that field carries no status code of its own."""
    fields = out.split(b'\0')
    entries = {}
    i = 0
    while i < len(fields):
        field = fields[i]
        i += 1
        if len(field) < 4: continue  # an empty tail field, or the old path this loop already skipped
        code = field[:2]
        entries[field[3:].decode('utf-8', 'replace')] = code.decode()
        if code[:1] in b'RC': i += 1  # the next field holds the old path of the rename or the copy
    return entries


def load_repo_status(root_dir: str):
    """Read every pending change of the repository at `root_dir` with ONE `git status`.

    Each local dependency then filters its own subfolder out of this result. The alternative asks git
    once per dependency, and a process costs about 23ms on Windows before git does any work. mama
    loads this before the dependency walk starts, so a parallel load reads it without a lock. The
    result stays empty when `root_dir` is not under git."""
    global _repo_status
    _repo_status = None
    top = _git_output(['rev-parse', '--show-toplevel'], root_dir).decode('utf-8', 'replace').strip()
    if not top: return
    entries = _parse_status(_git_output(['status', '--porcelain', '-z'], top))
    # Both sides of every later compare go through _case_key, so store the keys that way once.
    _repo_status = (_case_key(forward_slashes(top)), {_case_key(p): c for p, c in entries.items()})


def forget_repo_status():
    """Drop the shared status. A test that edits a working tree after the load calls this."""
    global _repo_status
    _repo_status = None


def _case_key(path: str) -> str:
    """The comparable spelling of a path. Windows matches a file name without case, so a compare that
    keeps the case reads `c:/projects` and `C:/Projects` as two different dirs. os.path.normcase is
    not usable here, because on Windows it also turns every forward slash into a back slash."""
    return path.lower() if System.windows else path


def _repo_status_kinds(src_dir: str):
    """(tracked edits, untracked files) under `src_dir`, read from the run's shared status. None when
    no status is loaded, when `src_dir` lies outside the repository the status covers, or when
    `src_dir` is a repository of its own.

    The last rule is a hard wall, and the caller must not be trusted to keep it. A git dependency
    clones into `<workspace>/<name>/<name>`, which sits under the root working tree, and .gitignore
    hides the whole workspace dir from the root status. That status therefore reports NO change for
    an edited clone, and mama would skip a rebuild the source needs."""
    if _repo_status is None or not memoize_git_fingerprints: return None
    top, entries = _repo_status
    src = _case_key(forward_slashes(os.path.abspath(src_dir)))
    if src != top and not src.startswith(top + '/'): return None
    if src != top and os.path.exists(path_join(src_dir, '.git')): return None  # a clone or a submodule
    prefix = '' if src == top else src[len(top)+1:] + '/'
    return _kinds_of({p: c for p, c in entries.items() if p.startswith(prefix)})


def _kinds_of(entries: dict) -> tuple:
    """(tracked changes, untracked files) from parsed `git status` entries, ignoring the files mama
    itself writes into a source dir. mama drops `mama.cmake` into every dependency, and counting it
    reported every clean dependency as dirty. Each one then paid for a `git ls-files` it did not need.
    A file the DEVELOPER left untracked still counts, which is the point of the check."""
    tracked = untracked = False
    for path, code in entries.items():
        if code != '??': tracked = True
        elif os.path.basename(path) not in _NON_SOURCE_ENTRIES: untracked = True
    return tracked, untracked


def forget_git_dir_fingerprint(src_dir: str):
    """Drop the memo of `src_dir`. run_git calls this after a git subcommand that can change the tree.
    Drops both keys, because the memo keys on the shared-status flag as well as the dir."""
    _git_fingerprints.pop((src_dir, False), None)
    _git_fingerprints.pop((src_dir, True), None)


def git_dir_fingerprint(src_dir: str, shared_status=False, reason='') -> str:
    """Cheap content-aware hash of uncommitted source under `src_dir`: tracked `git diff HEAD` scoped
    to this dir plus untracked file stats. '' for a clean tree, a dir not under git, or a missing dir.
    Lets `mama build` catch in-place source edits without a full status check or reconfigure.

    Three things keep the cost down. The run's shared status answers a local dependency, which then
    spawns no git at all. mama also memoizes the answer per source dir, because one build asks twice
    per dependency and the tree holds still between the two. A status then gates the two content
    commands, so a clean tree costs no process at all. A pinned dependency is clean.

    `shared_status` says this dir belongs to the root working tree, which only a local dependency can
    claim. A git dependency clones into the workspace dir, which .gitignore hides from the root status,
    so the root status would call every edited clone clean. `_repo_status_kinds` enforces that rule on
    its own, and this flag only spares a dir the check. The memo keys on the flag, so one wrong caller
    cannot store its answer under the key the right caller reads."""
    if not src_dir or not os.path.exists(src_dir):
        return ''
    if not memoize_git_fingerprints:
        return _compute_git_dir_fingerprint(src_dir, shared_status, reason)
    key = (src_dir, shared_status)
    if key not in _git_fingerprints:
        _git_fingerprints[key] = _compute_git_dir_fingerprint(src_dir, shared_status, reason)
    elif log_status_checks:
        # a memo hit knows the answer but not which kind of change made it, so it says neither
        _log_status_check(src_dir, reason, 'memo', 'dirty' if _git_fingerprints[key] else 'clean', 0.0)
    return _git_fingerprints[key]


def _compute_git_dir_fingerprint(src_dir: str, shared_status=False, reason='') -> str:
    """The git work behind git_dir_fingerprint, with no memo in front of it. Scoping every command with
    `-- .` keeps a subfolder of a larger repo from reading the parent's unrelated changes."""
    started = time.time()
    kinds = _repo_status_kinds(src_dir) if shared_status else None
    source = 'shared status'
    if kinds is None:  # no shared status covers this dir, so ask git about this dir alone
        # status reports the same set as the two content commands together: staged and unstaged changes
        # against HEAD, plus untracked files that .gitignore does not cover.
        kinds = _kinds_of(_parse_status(_git_output(['status', '--porcelain', '-z', '--', '.'], src_dir)))
        source = 'own git status'
    if log_status_checks: _log_status_check(src_dir, reason, source, _kinds_text(kinds), time.time() - started)
    tracked, untracked = kinds
    if not tracked and not untracked:
        return ''  # clean tree, and a pinned dependency almost always is
    # status already named WHICH kinds changed, so each content command runs only when it has work
    diff = _git_output(['diff', 'HEAD', '--', '.'], src_dir) if tracked else b''
    others = _git_output(['ls-files', '--others', '--exclude-standard', '-z', '--', '.'],
                         src_dir).decode('utf-8', 'replace') if untracked else ''
    if not diff and not others:
        return ''
    h = hashlib.sha1(diff)
    for rel in sorted(filter(None, others.split('\0'))):
        if os.path.basename(rel) in _NON_SOURCE_ENTRIES: continue  # mama wrote it, so it is not a source edit
        try:
            st = os.stat(path_join(src_dir, rel))
            h.update(f'\0{rel}\0{st.st_size}\0{st.st_mtime_ns}'.encode())
        except OSError:
            pass
    return h.hexdigest()[:16]
