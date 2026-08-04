import os, re, stat, shutil, tempfile, subprocess, hashlib
from functools import lru_cache
from typing import List
import time, pathlib, random
from .utils.system import System, Color, console, progress

# ssl, urllib.request, zipfile, datetime and dateutil are NOT imported here. They cost about 79ms
# together, every mama start pays it, and only download_file and unzip need them. Each of those
# imports its own. Keep it that way, and see tests/test_import_cost/.

MAMA_SHIM_FILENAME = 'mama_shim'


def has_shim_marker(directory: str) -> bool:
    """True if `directory` contains a mama_shim marker file."""
    return os.path.exists(os.path.join(directory, MAMA_SHIM_FILENAME))


def is_file_unmodified(src: str, dst: str):
    return os.path.getmtime(src) == os.path.getmtime(dst) and\
           os.path.getsize(src) == os.path.getsize(dst)


@lru_cache(maxsize=None)
def find_executable_from_system(name: str, follow_symlinks=False) -> str:
    """Absolute path to an executable, or an empty string when not found. Memoized: it reads every PATH
    directory, and PATH holds still for one mama run."""
    if not name: return ''
    output = shutil.which(name)
    if not output: return ''
    if follow_symlinks:
        output = os.path.realpath(output)
    return output if os.path.isfile(output) else ''


def copy_files(fromFolder: str, toFolder: str, fileNames: List[str]):
    for file in fileNames:
        sourceFile = path_join(fromFolder, file)
        if not os.path.exists(sourceFile):
            continue
        destFile = path_join(toFolder, os.path.basename(file))
        destFileExists = os.path.exists(destFile)
        if destFileExists and is_file_unmodified(sourceFile, destFile):
            console(f"skipping copy '{destFile}'")
            continue
        console(f"copyto '{toFolder}'  '{sourceFile}'")
        if System.windows and destFileExists: # note: windows crashes if dest file is in use
            tempCopy = f'{destFile}.{random.randrange(1000)}.deleted'
            shutil.move(destFile, tempCopy)
            try:
                os.remove(tempCopy)
            except Exception:
                pass
        shutil.copy2(sourceFile, destFile) # copy while preserving metadata


def deploy_framework(framework: str, deployFolder: str):
    if not os.path.exists(framework):
        raise IOError(f'no framework found at: {framework}')
    if os.path.exists(deployFolder):
        name = os.path.basename(framework)
        deployPath = path_join(deployFolder, name)
        console(f'Deploying framework to {deployPath}')
        remove_tree(deployPath)  # not `rm -rf`: a shell splits a path with a space and deletes the wrong tree
        shutil.copytree(framework, deployPath)
        return True
    return False


def has_contents_changed(filename: str, new_contents: str):
    if not os.path.exists(filename):
        return True
    return read_text_from(filename) != new_contents


def save_file_if_contents_changed(filename: str, new_contents: str) -> bool:
    if not has_contents_changed(filename, new_contents):
        return False
    write_text_to(filename, new_contents)
    return True


def path_join(first: str, *parts) -> str:
    """Join with forward/ slashes and keep the path exactly where it points. The relative sibling of
    normalized_join(): abspath() turns a relative path into a machine-specific absolute one, and on
    Windows it prepends the current drive. Use this for a path mama only prints, records, or hands to
    another tool. Use normalized_join() for a path mama opens on this machine."""
    result = first.rstrip('/\\')
    for part in parts:
        part = part.lstrip('/\\')
        if not part: continue
        result = f'{result.rstrip("/")}/{part}' if result else part
    return result


def forward_slashes(pathstring: str) -> str:
    """Replaces all back\\ slashes with forward/ slashes."""
    return pathstring.replace('\\', '/')


def file_sha1(path: str) -> str:
    """sha1 of a file's bytes. The one place mama identifies a file by content, so a recipe tag and a
    duplicate-tree report answer the same way."""
    with open(path, 'rb') as file:
        return hashlib.sha1(file.read()).hexdigest()


def short_path(path) -> str:
    """The last two parts of `path`, for a message that names a file. A consumer that sets mamafile=
    gets `mamadeps/qcoro.py`, the file it can edit, instead of a `qcoro/mamafile.py` that exists nowhere.
    '' for an empty path."""
    return '/'.join(forward_slashes(path).split('/')[-2:]) if path else ''


def back_slashes(pathstring: str) -> str:
    """Replaces all forward/ slashes with back\\ slashes."""
    return pathstring.replace('/', '\\')


def normalized_path(pathstring: str) -> str:
    """Normalizes a path to an ABSOLUTE path with all forward/ slashes."""
    pathstring = os.path.abspath(pathstring)
    return pathstring.replace('\\', '/').rstrip()


def normalized_join(path1: str, *pathsN) -> str:
    """Joins N paths and then calls normalized_path()."""
    return normalized_path(os.path.join(path1, *pathsN))


@lru_cache(maxsize=1)
def _cache_base() -> str:
    """The per-user cache dir of this platform. The temp dir when there is no home to put it in."""
    if System.windows: base = os.environ.get('LOCALAPPDATA', '')
    elif System.macos: base = os.path.expanduser('~/Library/Caches')
    else:              base = os.environ.get('XDG_CACHE_HOME') or os.path.expanduser('~/.cache')
    if not base or base.startswith('~'): base = tempfile.gettempdir()
    return path_join(base, 'mama')


@lru_cache(maxsize=None)
def user_cache_dir(*parts) -> str:
    """Cache dir for what belongs to this machine and this user, not to one workspace. The compiler seed
    is the example. MAMA_CACHE_DIR overrides the location. A CI job points it at a directory it keeps
    between runs, and a test points it at its own tmp dir. LOCALAPPDATA and an env override both arrive
    with back slashes, so the result goes through forward_slashes."""
    return forward_slashes(path_join(os.environ.get('MAMA_CACHE_DIR') or _cache_base(), *parts))


def glob_with_extensions(rootdir: str, extensions: List[str], exclude_dirs: List[str] = None) -> List[str]:
    results = []
    exclude = set(exclude_dirs) if exclude_dirs else None
    for dirpath, dirnames, dirfiles in os.walk(rootdir):
        if exclude: dirnames[:] = [d for d in dirnames if d not in exclude]  # prune generated/vendored trees
        for file in dirfiles:
            _, fext = os.path.splitext(file)
            if fext in extensions:
                results.append(normalized_join(dirpath, file))
    return results


def strstr_multi(s: str, substrings: List[str]) -> bool:
    if not substrings: # no substrings matches everything
        return True
    for substr in substrings:
        if substr in s:
            return True
    return False


def glob_with_name_match(rootdir: str, pattern_substrings: list, match_dirs=True) -> List[str]:
    results = []
    for dirpath, dirnames, dirfiles in os.walk(rootdir):
        if match_dirs:
            for dir in dirnames:
                if strstr_multi(dir, pattern_substrings):
                    results.append(normalized_join(dirpath, dir))
        for file in dirfiles:
            if strstr_multi(file, pattern_substrings):
                results.append(normalized_join(dirpath, file))
    return results


def glob_folders_with_name_match(rootdir: str, pattern_substrings: List[str]):
    results = []
    for dirpath, _, _ in os.walk(rootdir):
        if strstr_multi(dirpath, pattern_substrings):
            results.append(normalized_path(dirpath))
    return results


def remove_tree(dir: str):
    """Delete a directory tree, including a git clone. Windows refuses to unlink the read-only files git
    writes under `.git/objects/`, so make everything writable first. A missing dir is a no-op."""
    if not dir or not os.path.exists(dir): return
    if System.windows:
        for root, dirs, files in os.walk(dir):
            for d in dirs:  os.chmod(os.path.join(root, d), stat.S_IWUSR)
            for f in files: os.chmod(os.path.join(root, f), stat.S_IWUSR)
    shutil.rmtree(dir)


# Subdirs that prove a checkout is already here, even with no top-level file. A working tree whose root
# holds only directories used to read as empty, and mama then cloned over a good clone and failed.
_OCCUPIED_SUBDIRS = {'.git', 'include', 'src', 'lib', 'bin'}


def is_dir_empty(dir: str) -> bool:
    """True if there is nothing here worth keeping: no top-level file and no _OCCUPIED_SUBDIRS entry.
    The caller uses it to choose between cloning into `dir` and pulling what is already there."""
    if not os.path.exists(dir): return True
    _, dirnames, filenames = next(os.walk(dir))
    return not filenames and not any(d.lower() in _OCCUPIED_SUBDIRS for d in dirnames)


# Entries mama itself drops into a dep's src_dir, plus `.git` (metadata, not working-tree source).
_NON_SOURCE_ENTRIES = {'mama.cmake', '.git'}


def has_source_content(dir: str) -> bool:
    """True if `dir` holds anything mama did not put there - source a wipe would destroy. Counts subdirs
    (unlike is_dir_empty) and biases to 'source': worst case keeps a stale dir, never loses local work."""
    if not os.path.exists(dir): return False
    return any(entry not in _NON_SOURCE_ENTRIES for entry in os.listdir(dir))


def has_tag_changed(old_tag_file: str, new_tag: str):
    if not os.path.exists(old_tag_file):
        return True
    old_tag = read_text_from(old_tag_file)
    if old_tag != new_tag:
        console(f" tagchange '{old_tag.strip()}'\n"+
                f"      ---> '{new_tag.strip()}'")
        return True
    return False


def read_text_from(file_path: str) -> str:
    return pathlib.Path(file_path).read_text()


def write_text_to(file: str, text: str):
    dirname = os.path.dirname(file)
    if not os.path.exists(dirname):
        os.makedirs(dirname, exist_ok=True)
    pathlib.Path(file).write_text(text, encoding='utf-8')


def read_lines_from(file: str) -> List[str]:
    if not os.path.exists(file):
        return []
    with pathlib.Path(file).open(encoding='utf-8') as f:
        return f.readlines()


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


def get_file_size_str(size):
    """Returns the file size as a human readable string, eg 96.5KB or 100.1MB."""
    if size < 128: return f'{size}B' # only show bytes for really small < 0.1 KB sizes
    if size < (1024*1024): return f'{size/1024:.1f}KB'
    if size < (1024*1024*1024): return f'{size/(1024*1024):.1f}MB'
    return f'{size/(1024*1024):.2}GB'


def get_time_str(seconds: float):
    if seconds < 0.1: return f'{int(seconds*1000)}ms'  # ms only below 0.1s, because 0.2s reads better than 200ms
    if seconds < 60: return f'{seconds:.1f}s'
    if seconds < 60*60: return f'{int(seconds/60)}m {int(seconds%60)}s'
    if seconds < 24*60*60: return f'{int(seconds/(60*60))}h {int(seconds/60)%60}m {int(seconds)%60}s'
    return f'{int(seconds/(24*60*60))}d {int((seconds%(24*60*60))/(60*60))}h {int(seconds/60)%60}m {int(seconds)%60}s'


class ProgressBar:
    """In-place `|   <====| NN% (time)` bar: drawn on construction, committed by finish(). Redraws
    throttle by payload size (100MB every 1%, under ~1MB none) so small payloads never flicker."""
    def __init__(self, total: int, indent: str = '    '):
        self.total = total
        self.indent = indent
        self.interval = max(1, int((100*1024*1024) / total)) if total else 100
        self.start = time.time()
        self.done = 0
        self.percent = 0
        self.label = ''
        self._draw(0)  # via progress(), so a headless run throttles the opening bar like every redraw

    def _percent(self) -> int:
        return int((self.done / self.total) * 100.0) if self.total else 100

    def _tail(self) -> str:
        """Current item, truncated from the left so the informative filename tail survives."""
        if not self.label: return ''
        return f' {self.label}' if len(self.label) <= 32 else f' ...{self.label[-29:]}'

    def _draw(self, percent: int, final=False):
        n = int(percent / 2)
        bar = f'|{" "*(50-n)}<{"="*n}| {percent:>3}% ({get_time_str(time.time()-self.start)})'
        progress(f'{self.indent}{bar}{self._tail()}', final=final)

    def step(self, amount: int, label: str = ''):
        """Advances by `amount` bytes. `label` names the item in flight, shown on the next redraw."""
        self.done += amount
        self.label = label
        if self.interval >= 100: return
        percent = self._percent()
        if abs(self.percent - percent) < self.interval: return
        self.percent = percent
        self._draw(percent)

    def finish(self):
        """Commit the bar on its own line. Always drawn, even when redraws were throttled off, and
        reports the real percent so a truncated transfer is visible rather than claiming 100%."""
        self.label = ''  # at 100% there is no item in flight, so keep the committed line clean
        self._draw(self._percent(), final=True)


def download_file(remote_url:str, local_dir:str, force=False, message=None, name:str=None):
    """Downloads remote_url into local_dir. Returns the local file path, or None on failure.
    - force: [False] use any existing local file without contacting the server. When True, open the
      connection and compare Content-Length, and skip the body transfer when the sizes match.
    - message: [None] custom log line for the download start
    - name: [None] target name that prefixes the log lines under parallel updates"""
    import ssl  # deferred: ssl costs about 26ms to import, and only a download needs it
    from urllib import request
    local_file = normalized_join(local_dir, os.path.basename(remote_url))
    indent = f'  - {name: <16} ' if name else '    '
    if not force and os.path.exists(local_file):
        console(f'{indent}Using locally cached {local_file}')
        return local_file
    if not os.path.exists(local_dir):
        os.makedirs(local_dir, exist_ok=True)

    # TODO: this causes issues inside some secure networks
    if remote_url.startswith('https://'):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_OPTIONAL
    else:
        ctx = None

    with request.urlopen(remote_url, context=ctx, timeout=5) as urlfile:
        size = urlfile.info()['Content-Length']
        size = int(size.strip()) if size else None

        # size-match cache: one HTTP round-trip, already paid by opening the connection, saves the whole body
        if size is not None and os.path.exists(local_file) \
                and os.path.getsize(local_file) == size:
            console(f'{indent}Artifactory CACHE (size-match) '
                    f'{os.path.basename(local_file)} ({get_file_size_str(size)})')
            return local_file

        if not message: message = f'Downloading {remote_url}'
        console(f'{message} {get_file_size_str(size) if size else "unknown size"}')
        if not size:
            return None

        bar = ProgressBar(size, indent)
        transferred = 0
        with open(local_file, 'wb') as output:
            while transferred < size:
                data = urlfile.read(32*1024)
                if not data: break
                output.write(data)
                transferred += len(data)
                bar.step(len(data))

    bar.finish()
    return local_file


def unzip(local_zip: str, extract_dir: str, pwd: str = None):
    """Unzips an archive into extract_dir, throws on failure. Returns the number of files extracted.
    Extracts a file only when its size or modified time mismatches, preserves symlinks and permissions,
    and always sets the modified time from the zipfile info.
    - pwd: [None] archive password"""
    # deferred: zipfile and dateutil cost about 53ms together, and only an unzip needs them. The
    # nested annotations below resolve when this function runs, so the import must lead it.
    import zipfile
    from datetime import datetime
    from dateutil import tz
    def get_zipinfo_datetime(zipmember: zipfile.ZipInfo) -> datetime:
        zt = zipmember.date_time # tuple: year, month, day, hour, min, sec
        # ZIP uses localtime
        return datetime(zt[0], zt[1], zt[2], zt[3], zt[4], zt[5], tzinfo=tz.tzlocal())

    def has_file_changed(zipmember: zipfile.ZipInfo, dst_path):
        st: os.stat_result = None
        try:
            st = os.stat(dst_path, follow_symlinks=False)
            if st.st_size != zipmember.file_size:
                return True
            dst_mtime: datetime = datetime.fromtimestamp(st.st_mtime, tz=tz.tzlocal())
            src_mtime = get_zipinfo_datetime(zipmember)
            if dst_mtime != src_mtime:
                return True
        except (OSError, ValueError):
            return True # does not exist
        return False

    # creates a symlink only if necessary
    def make_symlink(zipmember: zipfile.ZipInfo, symlink_location, is_directory):
        target = zip.read(zipmember, pwd=pwd).decode('utf-8')
        # link does not exist, create it
        if not os.path.islink(symlink_location):
            if os.path.exists(symlink_location):
                os.remove(symlink_location)
            os.symlink(target, symlink_location, target_is_directory=is_directory)
            return True
        else:
            # only create if the link is different
            if os.readlink(symlink_location) != target:
                os.symlink(target, symlink_location, target_is_directory=is_directory)
                return True
        return False

    num_unzipped = 0

    with zipfile.ZipFile(local_zip, "r") as zip:
        for zipmember in zip.infolist():
            dst_path = os.path.normpath(os.path.join(extract_dir, zipmember.filename))
            mode = zipmember.external_attr >> 16
            is_symlink = stat.S_ISLNK(mode)
            did_extract = False
            if zipmember.is_dir():
                if is_symlink:
                    did_extract = make_symlink(zipmember, dst_path, is_directory=True)
                elif not os.path.isdir(dst_path):
                    os.makedirs(dst_path, exist_ok=True)
                    did_extract = True
            elif has_file_changed(zipmember, dst_path):  # only extract if file appears to be modified
                base_dir = os.path.dirname(dst_path)
                if not os.path.isdir(base_dir):
                    os.makedirs(base_dir)
                if is_symlink:
                    did_extract = make_symlink(zipmember, dst_path, is_directory=False)
                else:
                    with zip.open(zipmember, pwd=pwd) as src, open(dst_path, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                        did_extract = True
                if did_extract:
                    if not is_symlink:
                        perm = stat.S_IMODE(zipmember.external_attr >> 16)
                        os.chmod(dst_path, perm)
                    # set the modified time from the zip timestamp, so nothing reads as changed and rebuilds
                    time = get_zipinfo_datetime(zipmember)
                    mtime = time.timestamp()
                    if System.windows:
                        os.utime(dst_path, times=(mtime, mtime))
                    else:
                        os.utime(dst_path, times=(mtime, mtime), follow_symlinks=False)
            if did_extract:
                num_unzipped += 1

    return num_unzipped


def try_unzip(local_file:str, extract_dir:str) -> bool:
    """Attempts to unzip an archive. Returns (success: bool, num_extracted: int).
    (True, 0) means every destination file already matched the zip contents."""
    try:
        files_extracted = unzip(local_file, extract_dir)
        return (True, files_extracted)
    except zipfile.BadZipFile as e:
        return (False, -1)


def download_and_unzip(remote_file, extract_dir, local_file):
    if local_file and os.path.exists(local_file):
        console(f"Skipping {os.path.basename(remote_file)} because {local_file} exists.")
        return extract_dir
    local_file = download_file(remote_file, extract_dir)
    if not local_file:
        return None
    unzip(local_file, extract_dir)
    console(f'Extracted {local_file} to {extract_dir}')
    return extract_dir


def _should_copy(src: str, dst: str):
    if src == dst:
        return False # same file
    src_stat = None
    try:
        src_stat = os.stat(src)
    except (OSError, ValueError):
        return False # does not exist, nothing to copy

    dst_stat = None
    try:
        dst_stat = os.stat(dst)
    except (OSError, ValueError):
        return True # dst does not exist, so copy

    if src_stat.st_size != dst_stat.st_size:
        return True
    if src_stat.st_mtime != dst_stat.st_mtime:
        return True
    return False


def _passes_filter(src_file: str, filter) -> bool:
    if not filter: return True
    if callable(filter): return filter(src_file)
    if isinstance(filter, str): return src_file.endswith(filter)
    return any(src_file.endswith(f) for f in filter)


def copy_file(src: str, dst: str, filter=None) -> bool:
    """Copies a single file when it passes the filter and has changed. Returns True if copied.
    - filter: [None] a string suffix, a list of suffixes, or a function of the file path"""
    if _passes_filter(src, filter):
        if os.path.isdir(dst):
            dst = path_join(dst, os.path.basename(src))
        if _should_copy(src, dst):
            shutil.copyfile(src, dst, follow_symlinks=True)
            shutil.copystat(src, dst, follow_symlinks=True)
            return True
    return False


def copy_dir(src_dir: str, out_dir: str, filter=None, remap_root_dirname=False) -> bool:
    """Copies an entire dir. Copies each file only when it passes the filter and has changed.
    Returns True if any file was copied.
    - filter: [None] a string suffix, a list of suffixes, or a function of the file path
    - remap_root_dirname: [False] map src_dir contents directly into out_dir, so
      copy_dir('proj/src', 'deploy/include/mylib', remap_root_dirname=True) copies src/* -> include/mylib/*"""
    if not os.path.exists(src_dir):
        raise RuntimeError(f'copy_dir: {src_dir} does not exist!')
    if not os.path.exists(out_dir):
        os.makedirs(out_dir, exist_ok=True)
    copied = False
    root = src_dir if remap_root_dirname else os.path.dirname(src_dir)
    norm_out = os.path.normcase(os.path.normpath(out_dir))
    for fulldir, dirs, files in os.walk(src_dir):
        # skip the output directory to prevent infinite recursion
        dirs[:] = [d for d in dirs if os.path.normcase(os.path.normpath(os.path.join(fulldir, d))) != norm_out]

        reldir = fulldir[len(root):].lstrip('\\/')
        if reldir:
            dst_folder = path_join(out_dir, reldir)
            os.makedirs(dst_folder, exist_ok=True)
        else:
            dst_folder = out_dir
        for file in files:
            src_file = path_join(fulldir, file)
            dst_file = path_join(dst_folder, file)
            copied |= copy_file(src_file, dst_file, filter)
    return copied


def copy_if_needed(src: str, dst: str, filter=None) -> bool:
    """Copies src -> dst dir or file when needed. Returns True if anything was copied.
    - filter: [None] a string suffix, a list of suffixes, or a function of the file path"""
    if os.path.isdir(src):
        return copy_dir(src, dst, filter)
    else:
        return copy_file(src, dst, filter)


def is_network_error(e: Exception) -> bool:
    """True only if the exception clearly indicates network unavailability: DNS failure, connection
    refused or reset, timeout. False for auth errors (SSH key rejected, HTTP 401/403), HTTP 404,
    and anything ambiguous."""
    import subprocess, socket
    from urllib.error import HTTPError, URLError

    if isinstance(e, subprocess.TimeoutExpired):
        return True
    if isinstance(e, HTTPError):
        return False
    if isinstance(e, URLError):
        reason = getattr(e, 'reason', None)
        if isinstance(reason, (socket.timeout, socket.gaierror,
                               ConnectionRefusedError, ConnectionResetError,
                               TimeoutError, OSError)):
            return True
        return not isinstance(reason, str)
    if isinstance(e, (ConnectionRefusedError, ConnectionResetError,
                      TimeoutError, socket.timeout, socket.gaierror)):
        return True
    if isinstance(e, OSError):
        import errno
        if e.errno in (errno.ENETUNREACH, errno.EHOSTUNREACH,
                       errno.ECONNREFUSED, errno.ETIMEDOUT, errno.ECONNRESET):
            return True

    msg = str(e).lower()
    auth_patterns = [
        'permission denied', 'authentication failed',
        'host key verification failed',
        'returned error: 401', 'returned error: 403',
        'invalid credentials',
    ]
    for p in auth_patterns:
        if p in msg:
            return False
    network_patterns = [
        'could not resolve host', 'connection refused',
        'connection timed out', 'network is unreachable',
        'no route to host', 'name or service not known',
        'temporary failure in name resolution', 'connection reset',
    ]
    for p in network_patterns:
        if p in msg:
            return True
    return False



# git transfer progress ('Receiving objects: 42% (...)') classification - shared so every place that
# captures git output collapses the per-percent flood identically.
_GIT_PROGRESS = (('remote: Counting objects:', 'counting objects   '), ('remote: Compressing objects:', 'compressing objects'),
                 ('Receiving objects:', 'receiving objects  '), ('Resolving deltas:', 'resolving deltas   '),
                 ('Updating files:', 'updating files     '))


def git_progress_status(line: str):
    """(status label, percent) for a raw git transfer-progress line ('Receiving objects: 42%'), else None."""
    for needle, status in _GIT_PROGRESS:
        if needle in line:
            pct = line.split('%')[0].rsplit(':', 1)[-1].strip()
            return status, (int(pct) if pct.isdigit() else 0)
    return None


_PERCENT_RE = re.compile(r'\b\d{1,3}%')  # a NN% completion token: git 'Receiving objects: 42%', a
                                         # download bar '|===| 42%', mama's collapsed redraw, wget/curl, ...


def is_progress_line(line: str) -> bool:
    """True for any transfer/download progress update - a line carrying a NN% completion token.
    Consecutive such lines collapse to just the latest, so a progress bar (git, artifactory download,
    a custom build's own downloader) cannot flood a captured buffer with hundreds of per-percent updates."""
    return _PERCENT_RE.search(line) is not None


def parse_version(version: str) -> tuple:
    """'0.13.01' -> (0, 13, 1). Segments parse as ints so zero-padding is irrelevant and 0.13 ranks
    ABOVE 0.9 (a plain string compare gets that backwards). The parse drops non-numeric junk in a segment."""
    parts = []
    for segment in str(version).split('.'):
        digits = ''.join(c for c in segment if c.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts) if parts else (0,)


def version_at_least(current: str, required: str) -> bool:
    """True if `current` >= `required`, comparing segment-wise and zero-padding the shorter one
    (so '0.13' < '0.13.01')."""
    cur, req = parse_version(current), parse_version(required)
    width = max(len(cur), len(req))
    return cur + (0,) * (width - len(cur)) >= req + (0,) * (width - len(req))


class BuildError(RuntimeError):
    """An expected failure the USER has to fix (a broken build, an unreachable repo), not a mamabuild
    bug. Reported as a clean message with no Python traceback - a stack trace through mama's internals
    only buries the actual compiler, cmake or git error the user needs to read."""


class GitError(BuildError):
    """A git command failed. The message is a full report: the cause, the url, the command mama ran and
    the git lines that name the failure. See types/git_errors.format_git_failure."""
