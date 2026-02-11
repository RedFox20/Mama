import os
import shutil
import sys
from typing import Iterable

def init(caller_file: str = '', clean_dirs: Iterable[str] = []):
    # Needed for mama commands to perform work in the correct directory
    if caller_file:
        os.chdir(os.path.dirname(os.path.abspath(caller_file)))

    for d in clean_dirs:
        rmdir(d)

def shell_exec(cmd: str, exit_on_fail: bool = True, echo: bool = True) -> int:
    if echo: print(f'exec: {cmd}')
    result = os.system(cmd)
    if result != 0 and exit_on_fail:
        print(f'exec failed: code: {result} {cmd}')
        if result >= 255:
            result = 1
        sys.exit(result)
    return result

def file_contains(filepath: str, text: str) -> bool:
    with open(filepath, 'r') as f:
        content = f.read()
    return text in content

def file_exists(filepath: str) -> bool:
    return os.path.isfile(filepath)

def is_windows() -> bool:
    return os.name == 'nt'

def is_linux() -> bool:
    return os.name == 'posix' and sys.platform != 'darwin'

def onerror(func, path, _):
    import stat
    if not os.access(path, os.W_OK):
        os.chmod(path, stat.S_IWUSR)
        func(path)

def rmdir(path: str):
    if os.path.exists(path):
        shutil.rmtree(path, onerror=onerror)