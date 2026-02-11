import os
import shutil
import sys

def shell_exec(cmd, exit_on_fail=True, echo=True) -> int:
    if echo: print(f'exec: {cmd}')
    result = os.system(cmd)
    if result != 0 and exit_on_fail:
        print(f'exec failed: code: {result} {cmd}')
        if result >= 255:
            result = 1
        sys.exit(result)
    return result

def file_contains(filepath, text):
    with open(filepath, 'r') as f:
        content = f.read()
    return text in content

def file_exists(filepath):
    return os.path.isfile(filepath)

def is_windows():
    return os.name == 'nt'

def is_linux():
    return os.name == 'posix' and sys.platform != 'darwin'

def onerror(func, path, _):
    import stat
    if not os.access(path, os.W_OK):
        os.chmod(path, stat.S_IWUSR)
        func(path)

def rmtree(path: str):
    shutil.rmtree(path, onerror=onerror)