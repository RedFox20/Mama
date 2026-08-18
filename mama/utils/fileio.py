"""Read, write, compare and copy files and trees."""

import os, stat, shutil, pathlib, random, hashlib
from functools import lru_cache
from typing import List

from .system import System, console
from .paths import path_join


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


def file_sha1(path: str) -> str:
    """sha1 of a file's bytes. The one place mama identifies a file by content, so a recipe tag and a
    duplicate-tree report answer the same way."""
    with open(path, 'rb') as file:
        return hashlib.sha1(file.read()).hexdigest()


def read_text_from(file_path: str) -> str:
    return pathlib.Path(file_path).read_text()


def write_text_to(file: str, text: str):
    dirname = os.path.dirname(file)
    if not os.path.exists(dirname):
        os.makedirs(dirname, exist_ok=True)
    pathlib.Path(file).write_text(text, encoding='utf-8')


def read_lines_from(file: str, errors=None) -> List[str]:
    """- errors: [None] the codec error policy of `open`. Pass 'replace' to scan a file that a build
      tool reads as bytes, because a locale-encoded comment must not end the run."""
    if not os.path.exists(file):
        return []
    with pathlib.Path(file).open(encoding='utf-8', errors=errors) as f:
        return f.readlines()


def has_contents_changed(filename: str, new_contents: str):
    if not os.path.exists(filename):
        return True
    return read_text_from(filename) != new_contents


def save_file_if_contents_changed(filename: str, new_contents: str) -> bool:
    if not has_contents_changed(filename, new_contents):
        return False
    write_text_to(filename, new_contents)
    return True


def has_tag_changed(old_tag_file: str, new_tag: str):
    if not os.path.exists(old_tag_file):
        return True
    old_tag = read_text_from(old_tag_file)
    if old_tag != new_tag:
        console(f" tagchange '{old_tag.strip()}'\n"+
                f"      ---> '{new_tag.strip()}'")
        return True
    return False


def remove_tree(dir: str):
    """Delete a directory tree, including a git clone. Windows refuses to unlink the read-only files git
    writes under `.git/objects/`, so make everything writable first. A missing dir is a no-op."""
    if not dir or not os.path.exists(dir): return
    if System.windows:
        for root, dirs, files in os.walk(dir):
            for d in dirs:  os.chmod(os.path.join(root, d), stat.S_IWUSR)
            for f in files: os.chmod(os.path.join(root, f), stat.S_IWUSR)
    shutil.rmtree(dir)


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


_VCS_DIRS = ('.git', '.svn', '.hg')   # VCS metadata. A package never ships it, whatever a mamafile exports.


def _norm_path(path: str) -> str:
    """One spelling for a path, so a compare cannot fail on a detail. Windows ignores case, and `a/b`
    and `a\\b\\` name one dir. Both sides of a compare go through this."""
    return os.path.normcase(os.path.normpath(path))


def _prune_walk_dirs(walk_dir: str, dirs: list, skip_dir: str):
    """Remove every subdir this copy must not enter. Two rules apply. A VCS dir never ships in a
    package. The output dir would make the walk copy its own output, again and again.

    The slice assignment at the end is not a style choice. os.walk reads this same list back after it
    yields, and it descends into whatever is left. A plain `dirs = keep` would rebind the local name
    and change nothing.
    - walk_dir: the dir os.walk is visiting now, which names each subdir below
    - skip_dir: the output dir of this copy, already through _norm_path"""
    keep = []
    for d in dirs:
        if d in _VCS_DIRS: continue
        if _norm_path(path_join(walk_dir, d)) == skip_dir: continue
        keep.append(d)
    dirs[:] = keep


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
    norm_out = _norm_path(out_dir)
    for fulldir, dirs, files in os.walk(src_dir):
        _prune_walk_dirs(fulldir, dirs, norm_out)
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
