import os, runpy, inspect

from .utils.system import console
from .util import path_join, read_text_from, write_text_to, file_sha1

def parse_mamafile(config, target_class, mamafile):
    """Run `mamafile` and return (name, class) of its first `target_class` subclass."""
    if not mamafile or not os.path.exists(mamafile):
        return None, None
    loaded_globals = runpy.run_path(mamafile)
    for key, value in loaded_globals.items():
        if inspect.isclass(value) and issubclass(value, target_class):
            return key, value
    raise RuntimeError(f'No BuildTarget class found in mamafile: {mamafile}')


def _read_tag(tagfile):
    """(mtime, sha1) the last run recorded. A tag an older mama wrote holds the mtime alone."""
    try: lines = read_text_from(tagfile).split('\n')
    except OSError: return ('', '')
    return (lines[0].strip(), lines[1].strip() if len(lines) > 1 else '')


def _write_tag(tagfile, mtime, content_hash):
    write_text_to(tagfile, f'{mtime}\n{content_hash}')


def update_modification_tag(config, file, tagfile):
    """True when `file` changed since the last tag write. Refreshes the tag.

    Two layers. The mtime answers first and costs one stat, so an unchanged file opens nothing. Only a
    moved mtime reads the file, and the sha1 then decides. git rewrites a checked-out file with the same
    bytes, so the mtime alone would rebuild every dep that read the file."""
    if not os.path.exists(file):
        return False

    mtime = str(int(os.path.getmtime(file)))
    old_mtime, old_hash = _read_tag(tagfile)
    if mtime == old_mtime:
        if not old_hash: _write_tag(tagfile, mtime, file_sha1(file))  # upgrade a tag from an older mama
        if config.verbose: console(f'No Changes {file}')
        return False

    content_hash = file_sha1(file)
    _write_tag(tagfile, mtime, content_hash)
    if content_hash == old_hash:
        if config.verbose: console(f'No Changes {file} (mtime moved, same content)')
        return False

    if config.verbose: console(f'Update tagfile: {tagfile}')
    return True

def update_mamafile_tag(config, mamafile, build_dir):
    """True when mamafile.py changed since the last run."""
    mamafiletag = path_join(build_dir, 'mamafile_tag')
    return update_modification_tag(config, mamafile, mamafiletag)

def update_cmakelists_tag(config, cmakelists, build_dir):
    """True when CMakeLists.txt changed since the last run."""
    cmakeliststag = path_join(build_dir, 'cmakelists_tag')
    return update_modification_tag(config, cmakelists, cmakeliststag)
