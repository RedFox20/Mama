import os, stat, shutil, zipfile
from typing import List, Tuple
import time, ssl, pathlib, random
from .utils.system import System, console
from .utils.sub_process import execute
from urllib import request
from datetime import datetime
from dateutil import tz

def is_file_modified(src: str, dst: str):
    return os.path.getmtime(src) == os.path.getmtime(dst) and\
           os.path.getsize(src) == os.path.getsize(dst)


def find_executable_from_system(name: str):
    if not name: return ''
    output = shutil.which(name)
    if not output: return ''
    return output if os.path.isfile(output) else ''


def copy_files(fromFolder: str, toFolder: str, fileNames: List[str]):
    for file in fileNames:
        sourceFile = os.path.join(fromFolder, file)
        if not os.path.exists(sourceFile):
            continue
        destFile = os.path.join(toFolder, os.path.basename(file))
        destFileExists = os.path.exists(destFile)
        if destFileExists and is_file_modified(sourceFile, destFile):
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
        deployPath = os.path.join(deployFolder, name)
        console(f'Deploying framework to {deployPath}')
        execute(f'rm -rf {deployPath}')
        shutil.copytree(framework, deployPath)
        return True
    return False


def has_contents_changed(filename: str, new_contents: str):
    if not os.path.exists(filename):
        return True
    return read_text_from(filename) != new_contents


def save_file_if_contents_changed(filename: str, new_contents: str):
    if not has_contents_changed(filename, new_contents):
        return
    write_text_to(filename, new_contents)


def path_join(first: str, second: str) -> str:
    """ Always join with forward/ slashes """
    first  = first.rstrip('/\\')
    second = second.lstrip('/\\')
    if not first: return second
    if not second: return first
    return first + '/' + second


def forward_slashes(pathstring: str) -> str:
    """ Replace all back\\ slashes with forward/ slashes"""
    return pathstring.replace('\\', '/')


def back_slashes(pathstring: str) -> str:
    """ Replace all forward/ slashes with back\\ slashes"""
    return pathstring.replace('/', '\\')


def normalized_path(pathstring: str) -> str:
    """ Normalizes a path to ABSOLUTE path and all forward/ slashes """
    pathstring = os.path.abspath(pathstring)
    return pathstring.replace('\\', '/').rstrip()


def normalized_join(path1: str, *pathsN) -> str:
    """ Joins N paths and the calls normalized_path() """
    return normalized_path(os.path.join(path1, *pathsN))


def glob_with_extensions(rootdir: str, extensions: List[str]) -> List[str]:
    results = []
    for dirpath, _, dirfiles in os.walk(rootdir):
        for file in dirfiles:
            _, fext = os.path.splitext(file)
            if fext in extensions:
                pathstring = os.path.join(dirpath, file)
                pathstring = normalized_path(pathstring)
                results.append(pathstring)
    return results


def strstr_multi(s: str, substrings: List[str]) -> bool:
    #console(f'file: {s} matches: {substrings}')
    if not substrings: # if no substrings, then match everything
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
                    pathstring = os.path.join(dirpath, dir)
                    pathstring = normalized_path(pathstring)
                    results.append(pathstring)
        for file in dirfiles:
            if strstr_multi(file, pattern_substrings):
                pathstring = os.path.join(dirpath, file)
                pathstring = normalized_path(pathstring)
                results.append(pathstring)
    return results


def glob_folders_with_name_match(rootdir: str, pattern_substrings: List[str]):
    results = []
    for dirpath, _, _ in os.walk(rootdir):
        if strstr_multi(dirpath, pattern_substrings):
            results.append(normalized_path(dirpath))
    return results


def is_dir_empty(dir: str): # no files?
    if not os.path.exists(dir): return True
    _, _, filenames = next(os.walk(dir))
    return len(filenames) == 0


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


def get_file_size_str(size):
    """
    Returns file size as a human readable string, eg 96.5KB, or 100.1MB
    """
    if size < 128: return f'{size}B' # only show bytes for really small < 0.1 KB sizes
    if size < (1024*1024): return f'{size/1024:.1f}KB'
    if size < (1024*1024*1024): return f'{size/(1024*1024):.1f}MB'
    return f'{size/(1024*1024):.2}GB'


def get_time_str(seconds: float):
    if seconds < 1: return f'{int(seconds*1000)}ms'
    if seconds < 60: return f'{seconds:.1f}s'
    if seconds < 60*60: return f'{int(seconds%60)}m {int(seconds/60)}s'
    if seconds < 24*60*60: return f'{int(seconds%(60*60))}h {int(seconds%60)}m {int(seconds/60)}s'
    return f'{int(seconds%(24*60*60))}d {int(seconds%(60*60))}h {int(seconds%60)}m {int(seconds/60)}s'


def download_file(remote_url:str, local_dir:str, force=False, message=None):
    local_file = os.path.join(local_dir, os.path.basename(remote_url))
    if not force and os.path.exists(local_file): # download file?
        console(f"    Using locally cached {local_file}")
        return local_file
    start = time.time()
    if not os.path.exists(local_dir):
        os.makedirs(local_dir, exist_ok=True)

    # TODO: this causes issues inside some secure networks
    if remote_url.startswith('https://'):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_OPTIONAL
    else:
        ctx = None

    with request.urlopen(remote_url, context=ctx, timeout=15) as urlfile:
        size = int(urlfile.info()['Content-Length'].strip())
        if not message: message = f'Downloading {remote_url}'
        print(f'{message} {get_file_size_str(size) if size else "unknown size"}')
        if not size:
            return None

        # for 100MB file, interval = 1
        # for 10MB file, interval = 10
        # for 1MB file, interval = 100 (so essentially disabled)
        report_interval = max(1, int((100*1024*1024) / size))
        transferred = 0
        lastpercent = 0
        print(f'    |{" ":50}<| {0:>3}%', end='')
        with open(local_file, 'wb') as output:
            while transferred < size:
                data = urlfile.read(32*1024) # large chunks plz
                if not data: break
                output.write(data)
                transferred += len(data)
                if report_interval < 100:
                    percent = int((transferred / size) * 100.0)
                    if abs(lastpercent - percent) >= report_interval:
                        lastpercent = percent
                        n = int(percent / 2)
                        right = '=' * n
                        left = ' ' * int(50 - n)
                        elapsed = time.time() - start
                        print(f'\r    |{left}<{right}| {percent:>3}% ({get_time_str(elapsed)})', end='')

    # report actual percent here, just incase something goes wrong
    elapsed = time.time() - start
    percent = int((transferred / size) * 100.0)
    print(f'\r    |<{"="*50}| {percent:>3}% ({get_time_str(elapsed)})')
    return local_file


def unzip(local_zip: str, extract_dir: str, pwd: str = None):
    """
    Attempts to unzip an archive, throws on failure.
    Only extracts the files if their current size or modified time mismatches.
    Always sets modified time from the zipfile info.
    Preserves symlinks. And sets the correct file permission attributes.
    Returns # of files actually extracted.
    """
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

    def make_symlink(zipmember: zipfile.ZipInfo, symlink_location, is_directory):
        target = zip.read(zipmember, pwd=pwd).decode('utf-8')
        if os.path.lexists(symlink_location):
            os.remove(symlink_location)
        os.symlink(target, symlink_location, target_is_directory=is_directory)

    unzipped_files: List[Tuple[zipfile.ZipFile, str]] = []

    with zipfile.ZipFile(local_zip, "r") as zip:
        for zipmember in zip.infolist():
            dst_path = os.path.normpath(os.path.join(extract_dir, zipmember.filename))
            mode = zipmember.external_attr >> 16
            is_symlink = stat.S_ISLNK(mode)
            #what = 'DIR' if zipmember.is_dir() else 'FILE'
            #what = what + ' LINK' if is_symlink else what
            #print(f'{what} {zipmember.filename} S_IMODE={stat.S_IMODE(mode):0o} S_IFMT={stat.S_IFMT(mode):0o}')
            if zipmember.is_dir():  # make dirs if needed
                if is_symlink:
                    make_symlink(zipmember, dst_path, is_directory=True)
                else:
                    os.makedirs(dst_path, exist_ok=True)
            elif has_file_changed(zipmember, dst_path):  # only extract if file appears to be modified
                unzipped_files.append((zipmember, dst_path))
                if is_symlink:
                    make_symlink(zipmember, dst_path, is_directory=False)
                else:
                    with zip.open(zipmember, pwd=pwd) as src, open(dst_path, "wb") as dst:
                        shutil.copyfileobj(src, dst)
        for zipmember, dst_path in unzipped_files:
            # set the correct permissions for files and folders
            perm = stat.S_IMODE(zipmember.external_attr >> 16)
            os.chmod(dst_path, perm)
            # always set the modification date from the zipmember timestamp,
            # this way we can avoid unnecessarily modifying files and causing full rebuilds
            time = get_zipinfo_datetime(zipmember)
            #print(f'    | {dst_path} {time}')
            mtime = time.timestamp()
            if System.windows:
                os.utime(dst_path, times=(mtime, mtime))
            else:
                os.utime(dst_path, times=(mtime, mtime), follow_symlinks=False)

    return len(unzipped_files)


def try_unzip(local_file:str, extract_dir:str) -> bool:
    """
    Attempts to unzip an archive, returns a tuple (success: bool, num_extracted: int)
    If (success: True, num_extracted: 0) is returned, it means none of the destination files
    were different from the zip contents, and zero extractions were performed
    """
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
        return True # dst doesn't exist, definitely need to copy it

    if src_stat.st_size != dst_stat.st_size:
        #console(f'_should_copy true src.size != dst.size\n┌──{src}\n└─>{dst}')
        return True
    if src_stat.st_mtime != dst_stat.st_mtime:
        #console(f'_should_copy true src.mtime != dst.mtime\n┌<──{src}\n└──> {dst}')
        return True
    #console(f'skip {dst}')
    return False


def _passes_filter(src_file: str, filter: str|List[str]|None) -> bool:
    if not filter:
        return True
    if isinstance(filter, str):
        return src_file.endswith(filter)
    for f in filter:
        if src_file.endswith(f):
            return True
    return False


def copy_file(src: str, dst: str, filter: str|List[str]|None = None) -> bool:
    """
        Copies a single file if it passes the filter and
        if it has changed, returns TRUE if copied
    """
    if _passes_filter(src, filter):
        if os.path.isdir(dst):
            dst = os.path.join(dst, os.path.basename(src))
        if _should_copy(src, dst):
            #console(f'copy {src}\n --> {dst}')
            shutil.copyfile(src, dst, follow_symlinks=True)
            shutil.copystat(src, dst, follow_symlinks=True)
            return True
    return False


def copy_dir(src_dir: str, out_dir: str, filter: str|List[str]|None = None) -> bool:
    """
        Copies an entire dir if it passes the filter and
        if the individual files have changed.
        Returns TRUE if any files were copied.
    """
    if not os.path.exists(src_dir):
        raise RuntimeError(f'copy_dir: {src_dir} does not exist!')
    copied = False
    root = os.path.dirname(src_dir)
    for fulldir, _, files in os.walk(src_dir):
        reldir = fulldir[len(root):].lstrip('\\/')
        if reldir:
            dst_folder = os.path.join(out_dir, reldir)
            os.makedirs(dst_folder, exist_ok=True)
        else:
            dst_folder = out_dir
        for file in files:
            src_file = os.path.join(fulldir, file)
            dst_file = os.path.join(dst_folder, file)
            copied |= copy_file(src_file, dst_file, filter)
    return copied


def copy_if_needed(src: str, dst: str, filter: str|List[str]|None = None) -> bool:
    """ Copies src -> dst  dir/file  if needed and returns TRUE if anything was copied """
    #console(f'COPY {src} --> {dst}')
    if os.path.isdir(src):
        return copy_dir(src, dst, filter)
    else:
        return copy_file(src, dst, filter)

