"""Path spelling and directory tests. The CHEAPEST utils module: it imports os and nothing costly,
so every other module may depend on it. Keep it that way, and see tests/test_import_cost/."""

import os, tempfile
from functools import lru_cache
from typing import List

from .system import System

MAMA_SHIM_FILENAME = 'mama_shim'

# Subdirs that prove a checkout is already here, even with no top-level file. A working tree whose root
# holds only directories used to read as empty, and mama then cloned over a good clone and failed.
_OCCUPIED_SUBDIRS = {'.git', 'include', 'src', 'lib', 'bin'}

# Entries mama itself drops into a dep's src_dir, plus `.git` (metadata, not working-tree source).
_NON_SOURCE_ENTRIES = {'mama.cmake', '.git'}


def has_shim_marker(directory: str) -> bool:
    """True if `directory` contains a mama_shim marker file."""
    return os.path.exists(os.path.join(directory, MAMA_SHIM_FILENAME))


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


def back_slashes(pathstring: str) -> str:
    """Replaces all forward/ slashes with back\\ slashes."""
    return pathstring.replace('/', '\\')


def short_path(path) -> str:
    """The last two parts of `path`, for a message that names a file. A consumer that sets mamafile=
    gets `mamadeps/qcoro.py`, the file it can edit, instead of a `qcoro/mamafile.py` that exists nowhere.
    '' for an empty path."""
    return '/'.join(forward_slashes(path).split('/')[-2:]) if path else ''


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


_MAMA_DIR = '.mama'  # one hidden entry per workspace, so `ls packages/` shows packages and nothing else


def workspace_mama_dir(workspace: str, *parts) -> str:
    """What mama keeps inside a workspace but never ships: the lock sidecars and the compiler seeds.
    Everything lives under one `.mama` dir, so `rm -rf packages/` still heals all of it at once."""
    return path_join(workspace, _MAMA_DIR, *parts)


@lru_cache(maxsize=None)
def user_cache_dir(*parts) -> str:
    """Cache dir for what belongs to this machine and this user, not to one workspace. The compiler seed
    is the example. MAMA_CACHE_DIR overrides the location. A CI job points it at a directory it keeps
    between runs, and a test points it at its own tmp dir. LOCALAPPDATA and an env override both arrive
    with back slashes, so the result goes through forward_slashes."""
    return forward_slashes(path_join(os.environ.get('MAMA_CACHE_DIR') or _cache_base(), *parts))


def glob_with_extensions(rootdir: str, extensions: List[str], exclude_dirs: List[str] = None,
                         recursive=True) -> List[str]:
    results = []
    exclude = set(exclude_dirs) if exclude_dirs else None
    for dirpath, dirnames, dirfiles in os.walk(rootdir):
        if exclude: dirnames[:] = [d for d in dirnames if d not in exclude]  # prune generated/vendored trees
        if not recursive: dirnames.clear()  # os.walk reads this list back, so an empty one stops the descent
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


def is_dir_empty(dir: str) -> bool:
    """True if there is nothing here worth keeping: no top-level file and no _OCCUPIED_SUBDIRS entry.
    The caller uses it to choose between cloning into `dir` and pulling what is already there."""
    if not os.path.exists(dir): return True
    _, dirnames, filenames = next(os.walk(dir))
    return not filenames and not any(d.lower() in _OCCUPIED_SUBDIRS for d in dirnames)


def has_source_content(dir: str) -> bool:
    """True if `dir` holds anything mama did not put there - source a wipe would destroy. Counts subdirs
    (unlike is_dir_empty) and biases to 'source': worst case keeps a stale dir, never loses local work."""
    if not os.path.exists(dir): return False
    return any(entry not in _NON_SOURCE_ENTRIES for entry in os.listdir(dir))
