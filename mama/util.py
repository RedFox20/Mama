import os, shutil, random, shlex, time, subprocess, pathlib, ssl, zipfile
from mama.system import System, console, execute
from urllib import request


def is_file_modified(src, dst):
    return os.path.getmtime(src) == os.path.getmtime(dst) and\
           os.path.getsize(src) == os.path.getsize(dst)


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


def run_with_timeout(executable, argstring, workingDir, timeoutSeconds=None):
    args = [executable]
    args += shlex.split(argstring)
    start = time.time()
    proc = subprocess.Popen(args, shell=True, cwd=workingDir)
    try:
        proc.wait(timeout=timeoutSeconds)
        console(f'{executable} elapsed: {round(time.time()-start, 1)}s')
    except subprocess.TimeoutExpired:
        console('TIMEOUT, sending break signal')
        if System.windows:
            proc.send_signal(subprocess.signal.CTRL_C_EVENT)
        else:
            proc.send_signal(subprocess.signal.SIGINT)
        raise
    if proc.returncode == 0:
        return
    raise subprocess.CalledProcessError(proc.returncode, ' '.join(args))


def has_contents_changed(filename, new_contents):
    if not os.path.exists(filename):
        return True
    return pathlib.Path(filename).read_text() != new_contents


def save_file_if_contents_changed(filename, new_contents):
    if not has_contents_changed(filename, new_contents):
        return
    pathlib.Path(filename).write_text(new_contents)


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
    old_tag = pathlib.Path(old_tag_file).read_text()
    if old_tag != new_tag:
        console(f" tagchange '{old_tag.strip()}'\n"+
                f"      ---> '{new_tag.strip()}'")
        return True
    return False


def write_text_to(file, text):
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


def download_file(remote_url, local_dir, force=False, message=None):
    local_file = os.path.join(local_dir, os.path.basename(remote_url))
    if not force and os.path.exists(local_file): # download file?
        console(f"    Using locally cached {local_file}")
        return local_file
    if not os.path.exists(local_dir):
        os.makedirs(local_dir, exist_ok=True)

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    with request.urlopen(remote_url, context=ctx) as urlfile:
        with open(local_file, 'wb') as output:
            size = int(urlfile.info()['Content-Length'].strip())
            if not message: message = f'Downloading {remote_url}'
            print(f'{message} {get_file_size_str(size)}')
            print(f'    |{" ":50}<| {0:>3} %', end='')
            transferred = 0
            lastpercent = 0
            while True:
                data = urlfile.read(32*1024) # large chunks plz
                if not data:
                    print(f'\r    |<{"="*50}| {percent:>3} %')
                    return local_file
                output.write(data)
                transferred += len(data)
                percent = int((transferred / size) * 100.0)
                if abs(lastpercent - percent) >= 5: # report every 5%
                    lastpercent = percent
                    n = int(percent / 2)
                    right = '=' * n
                    left = ' ' * int(50 - n)
                    print(f'\r    |{left}<{right}| {percent:>3} %', end='')


def unzip(local_zip, extract_dir):
    with zipfile.ZipFile(local_zip, "r") as zip:
        zip.extractall(extract_dir)


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
        #console(f'copy {src}\n --> {dst}')
        return True
    if src_stat.st_mtime != dst_stat.st_mtime:
        return True
    #console(f'skip {dst}')
    return False


def _passes_filter(src_file, filter):
    if not filter: return True
    for f in filter:
        if src_file.endswith(f):
            return True
    return False


def copy_file(src, dst, filter):
    if _passes_filter(src, filter) and _should_copy(src, dst):
        #console(f'copy {src}\n --> {dst}')
        shutil.copy2(src, dst)


def copy_dir(src_dir, out_dir, filter=None):
    if not os.path.exists(src_dir):
        raise RuntimeError(f'copy_dir: {src_dir} does not exist!')
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
            copy_file(src_file, dst_file, filter)


def copy_if_needed(src, dst, filter=None):
    #console(f'COPY {src} --> {dst}')
    if os.path.isdir(src):
        copy_dir(src, dst, filter)
    else:
        copy_file(src, dst, filter)

