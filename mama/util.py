import os, sys, shutil, random, pathlib, ssl, zipfile
import time
from .utils.system import System, console
from .utils.sub_process import execute
from urllib import request


def is_file_modified(src, dst):
    return os.path.getmtime(src) == os.path.getmtime(dst) and\
           os.path.getsize(src) == os.path.getsize(dst)


def find_executable_from_system(name):
    if not name: return ''
    output = shutil.which(name)
    if not output: return ''
    return output if os.path.isfile(output) else ''


def copy_files(fromFolder, toFolder, fileNames):
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


def deploy_framework(framework, deployFolder):
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


def has_contents_changed(filename, new_contents):
    if not os.path.exists(filename):
        return True
    return read_text_from(filename) != new_contents


def save_file_if_contents_changed(filename, new_contents):
    if not has_contents_changed(filename, new_contents):
        return
    write_text_to(filename, new_contents)


def path_join(first, second):
    """ Always join with forward/ slashes """
    first  = first.rstrip('/\\')
    second = second.lstrip('/\\')
    if not first: return second
    if not second: return first
    return first + '/' + second


def forward_slashes(pathstring):
    """ Replace all back\\ slashes with forward/ slashes"""
    return pathstring.replace('\\', '/')


def back_slashes(pathstring):
    """ Replace all forward/ slashes with back\\ slashes"""
    return pathstring.replace('/', '\\')


def normalized_path(pathstring):
    """ Normalizes a path to ABSOLUTE path and all forward/ slashes """
    pathstring = os.path.abspath(pathstring)
    return pathstring.replace('\\', '/').rstrip()


def normalized_join(path1, *pathsN):
    """ Joins N paths and the calls normalized_path() """
    return normalized_path(os.path.join(path1, *pathsN))


def glob_with_extensions(rootdir, extensions):
    results = []
    for dirpath, _, dirfiles in os.walk(rootdir):
        for file in dirfiles:
            _, fext = os.path.splitext(file)
            if fext in extensions:
                pathstring = os.path.join(dirpath, file)
                pathstring = normalized_path(pathstring)
                results.append(pathstring)
    return results


def strstr_multi(s, substrings):
    #console(f'file: {s} matches: {substrings}')
    if not substrings: # if no substrings, then match everything
        return True
    for substr in substrings:
        if substr in s:
            return True
    return False


def glob_with_name_match(rootdir, pattern_substrings, match_dirs=True):
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


def glob_folders_with_name_match(rootdir, pattern_substrings):
    results = []
    for dirpath, _, _ in os.walk(rootdir):
        if strstr_multi(dirpath, pattern_substrings):
            results.append(normalized_path(dirpath))
    return results


def is_dir_empty(dir): # no files?
    if not os.path.exists(dir): return True
    _, _, filenames = next(os.walk(dir))
    return len(filenames) == 0


def has_tag_changed(old_tag_file, new_tag):
    if not os.path.exists(old_tag_file):
        return True
    old_tag = read_text_from(old_tag_file)
    if old_tag != new_tag:
        console(f" tagchange '{old_tag.strip()}'\n"+
                f"      ---> '{new_tag.strip()}'")
        return True
    return False


def read_text_from(file_path):
    return pathlib.Path(file_path).read_text()


def write_text_to(file, text):
    dirname = os.path.dirname(file)
    if not os.path.exists(dirname):
        os.makedirs(dirname, exist_ok=True)
    pathlib.Path(file).write_text(text, encoding='utf-8')


def read_lines_from(file):
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


def unzip(local_zip, extract_dir):
    """ Attempts to unzip an archive, throws on failure """
    with zipfile.ZipFile(local_zip, "r") as zip:
        zip.extractall(extract_dir)


def try_unzip(local_file:str, build_dir:str) -> bool:
    try:
        unzip(local_file, build_dir)
        return True
    except zipfile.BadZipFile as e:
        return False


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


def _should_copy(src, dst):
    if not os.path.exists(dst):
        return True

    src_stat = os.stat(src)
    dst_stat = os.stat(dst)

    if src_stat.st_size != dst_stat.st_size:
        #console(f'_should_copy true src.size != dst.size\n┌──{src}\n└─>{dst}')
        return True
    if src_stat.st_mtime != dst_stat.st_mtime:
        #console(f'_should_copy true src.mtime != dst.mtime\n┌<──{src}\n└──> {dst}')
        return True
    #console(f'skip {dst}')
    return False


def _passes_filter(src_file, filter):
    if not filter: return True
    for f in filter:
        if src_file.endswith(f):
            return True
    return False


def copy_file(src, dst, filter) -> bool:
    """
        Copies a single file if it passes the filter and
        if it has changed, returns TRUE if copied
    """
    if _passes_filter(src, filter) and _should_copy(src, dst):
        #console(f'copy {src}\n --> {dst}')
        shutil.copy2(src, dst)
        return True
    return False


def copy_dir(src_dir, out_dir, filter=None) -> bool:
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


def copy_if_needed(src, dst, filter=None) -> bool:
    """ Copies src -> dst  dir/file  if needed and returns TRUE if anything was copied """
    #console(f'COPY {src} --> {dst}')
    if os.path.isdir(src):
        return copy_dir(src, dst, filter)
    else:
        return copy_file(src, dst, filter)

