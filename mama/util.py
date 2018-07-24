import os, shutil, random, shlex, time, subprocess, pathlib, ssl, urllib, zipfile
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


# always join with forward slash /
def path_join(first, second):
    first  = first.rstrip('/\\')
    second = second.lstrip('/\\')
    if not first: return second
    if not second: return first
    return first + '/' + second


def forward_slashes(pathstring):
    return pathstring.replace('\\', '/')


def back_slashes(pathstring):
    return pathstring.replace('/', '\\')


def normalized_path(pathstring):
    pathstring = os.path.abspath(pathstring)
    return pathstring.replace('\\', '/').rstrip()


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
    for substr in substrings:
        if substr in s:
            return True
    return False


def glob_with_name_match(rootdir, pattern_substrings):
    results = []
    for dirpath, _, dirfiles in os.walk(rootdir):
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
    pathlib.Path(file).write_text(text)


def read_lines_from(file):
    if not os.path.exists(file):
        return []
    with pathlib.Path(file).open() as f:
        return f.readlines()


def download_file(remote_url, local_dir, force=False):
    local_file = os.path.join(local_dir, os.path.basename(remote_url))
    if not force and os.path.exists(local_file): # download file?
        console(f"Using locally cached {local_file}")
        return local_file
    if not os.path.exists(local_dir):
        os.makedirs(local_dir, exist_ok=True)
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with request.urlopen(remote_url, context=ctx) as urlfile:
        with open(local_file, 'wb') as output:
            total = int(urlfile.info()['Content-Length'].strip())
            total_megas = int(total/(1024*1024))
            prev_progress = -100
            written = 0
            while True:
                data = urlfile.read(32*1024) # large chunks plz
                if not data:
                    console(f"\rDownload {remote_url} finished.                 ")
                    return local_file
                output.write(data)
                written += len(data)
                progress = int((written*100)/total)
                if (progress - prev_progress) >= 5: # report every 5%
                    prev_progress = progress
                    written_megas = int(written/(1024*1024))
                    console(f"\rDownloading {remote_url} {written_megas}/{total_megas}MB ({progress}%)...")


def download_and_unzip(remote_zip, extract_dir):
    local_file = download_file(remote_zip, extract_dir)
    with zipfile.ZipFile(local_file, "r") as zip:
        zip.extractall(extract_dir)

