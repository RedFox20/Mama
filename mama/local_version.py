"""The version mama computes for a local module that pins none. See docs/roadmap-local-module-version.md.

A local dependency needs no pre-clone reader, because its source is already on disk. So mama names it
by content, and both the download side and the upload side run the same function over the same tree.
"""

from __future__ import annotations
import hashlib, json, os
from typing import TYPE_CHECKING

from . import build_names
from .util import path_join, normalized_path, read_text_from, save_file_if_contents_changed

if TYPE_CHECKING:
    from .build_dependency import BuildDependency


_MEMO_FILE = 'src_version_memo'
_TEXT_PROBE = 8000  # git reads this many bytes to decide whether a file is text

# The version field of a local module reads `local-<digest>`, so an artifactory listing says at a
# glance which packages a source tree named and which a commit or a tag named.
VERSION_PREFIX = 'local-'
# 10 hex digits is 40 bits. The archive name already separates modules, platforms, compilers, arches
# and build types, so one namespace holds the published builds of ONE module for ONE config. At 1000
# of those the odds of a collision are 1 in 2.2 million, and at 10000 they are 1 in 22 thousand.
_DIGEST_CHARS = 10

# Dirs and suffixes that never change what a build produces. Kept short on purpose.
# `_NON_LIB_DIRS` in build_target.py is NOT usable here. It drops test, docs, external and
# third_party, and an edit to any of those can change the library mama ships.
_IGNORED_DIRS = {'.git', '.svn', '.mama', '__pycache__', '.vs', '.vscode', '.idea'}
_IGNORED_SUFFIXES = {'.o', '.obj', '.a', '.so', '.dll', '.dylib', '.lib', '.pyc', '.pdb', '.ilk', '.exp'}


def compute_version(dep: BuildDependency) -> str:
    """The `local-<10 hex digits>` name of this module, from the content of its source tree.

    Content, not mtime: a fresh checkout stamps every file with the checkout time, so two machines
    would never agree on an mtime name. Content makes every clean checkout of one commit agree.

    ONE function spells this value. The download side and the upload side both call it over the same
    tree on the same disk, so the two can never disagree about the name of the archive."""
    entries = []
    memo = _load_memo(dep)
    fresh = {}
    for full_path, rel_path in _source_files(dep):
        file_hash = _file_hash(full_path, memo, fresh)
        if file_hash: entries.append((rel_path, file_hash))
    _save_memo(dep, fresh)
    h = hashlib.sha1()
    for rel_path, file_hash in sorted(entries):  # sorted, so the walk order never changes the value
        h.update(rel_path.encode('utf-8'))
        h.update(b'\0')  # a path holds no NUL, so this keeps ('ab','c') and ('a','bc') apart
        h.update(file_hash)
    return VERSION_PREFIX + h.hexdigest()[:_DIGEST_CHARS]


def is_publishable(dep: BuildDependency) -> bool:
    """True when this module has no uncommitted edit, so another machine can reproduce the version.
    A dirty tree still builds and still gets a version. It only refuses to publish."""
    return not dep.dep_source.working_tree_fingerprint(dep)


def ignored_dirs(dep: BuildDependency) -> set:
    """Dir names the walk skips. The workspace dir and the build dir come from config, because mama
    already names both, and a project-local workspace puts them inside the source tree."""
    names = set(_IGNORED_DIRS)
    names.add(getattr(dep, 'workspace', None) or 'packages')
    names.add(build_names.build_dir_name(dep.config, dep.variant_suffix))
    return names


def _source_files(dep: BuildDependency):
    """(full path, relative path) for every file that can change what this module builds."""
    root = normalized_path(dep.src_dir)
    skip = ignored_dirs(dep)
    for current, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in skip]
        for name in files:
            if os.path.splitext(name)[1].lower() in _IGNORED_SUFFIXES: continue
            full_path = path_join(current, name)
            yield full_path, os.path.relpath(full_path, root).replace(os.sep, '/')


def _file_hash(full_path: str, memo: dict, fresh: dict) -> bytes:
    """The sha1 digest of one file, from the memo when its size and mtime still match. A warm run then
    opens no file at all, which is the difference between 10ms and 2.6ms on a 170 file tree."""
    try:
        st = os.stat(full_path)
    except OSError:
        return b''  # a file that vanished between the walk and the stat names nothing
    key = f'{st.st_size}:{st.st_mtime_ns}'
    stored = memo.get(full_path)
    if stored and stored[0] == key:
        fresh[full_path] = stored
        return bytes.fromhex(stored[1])
    try:
        with open(full_path, 'rb') as f: data = f.read()
    except OSError:
        return b''
    # git folds CRLF to LF when it stores a text file, and a checkout may put the CR back. Two machines
    # must not name one source twice, so mama hashes the folded form. This is the text rule git uses.
    if b'\0' not in data[:_TEXT_PROBE]: data = data.replace(b'\r\n', b'\n')
    digest = hashlib.sha1(data).digest()
    fresh[full_path] = (key, digest.hex())
    return digest


def _memo_file(dep: BuildDependency) -> str:
    return path_join(dep.build_dir, _MEMO_FILE)


def _load_memo(dep: BuildDependency) -> dict:
    """{path: (size:mtime, hex digest)} from the last run. Empty on `mama update`, because a fetch can
    write new content under an old mtime. Empty on any read error, because a memo miss only costs a read."""
    if dep.config.update: return {}
    try:
        stored = json.loads(read_text_from(_memo_file(dep)) or '{}')
        return {path: tuple(value) for path, value in stored.items() if len(value) == 2}
    except Exception:
        return {}


def _save_memo(dep: BuildDependency, fresh: dict):
    """Write the memo back, and never fail a build over it. Only the files this run visited stay, so a
    deleted file leaves the memo on the next run."""
    try:
        save_file_if_contents_changed(_memo_file(dep), json.dumps(fresh, sort_keys=True))
    except Exception:
        pass
