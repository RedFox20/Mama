"""Unzip an archive. zipfile and dateutil cost about 53ms to import, so both stay inside the
functions that need them. See tests/test_import_cost/."""

import os, stat, shutil

from .system import System


def unzip(local_zip: str, extract_dir: str, pwd: str = None):
    """Unzips an archive into extract_dir, throws on failure. Returns the number of files extracted.
    Extracts a file only when its size or modified time mismatches, preserves symlinks and permissions,
    and always sets the modified time from the zipfile info.
    - pwd: [None] archive password"""
    # deferred: the nested annotations below resolve when this function runs, so the import must lead them
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
    import zipfile  # deferred, the same way unzip() does it
    try:
        files_extracted = unzip(local_file, extract_dir)
        return (True, files_extracted)
    except zipfile.BadZipFile as e:
        return (False, -1)
