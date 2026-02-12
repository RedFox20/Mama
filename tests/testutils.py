import os
import shutil
import subprocess
import sys
from typing import Iterable

import pytest

def init(caller_file: str = '', clean_dirs: Iterable[str] = []):
    # Needed for mama commands to perform work in the correct directory
    if caller_file:
        os.chdir(os.path.dirname(os.path.abspath(caller_file)))

    for d in clean_dirs:
        rmdir(d)

def shell_exec(cmd: str, exit_on_fail: bool = True, echo: bool = True) -> int:
    if echo: print(f'exec: {cmd}')
    result = subprocess.run(cmd, shell=True)
    if result.returncode != 0 and exit_on_fail:
        pytest.fail(f'exec failed: code: {result.returncode} {cmd}')
    return result.returncode

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

def is_macos() -> bool:
    return sys.platform == 'darwin'

def executable_extension() -> str:
    if is_windows():
        return '.exe'

    return ''

def static_library_extension() -> str:
    if is_windows():
        return '.lib'
    else:
        return '.a'

def dynamic_library_extension() -> str:
    if is_windows():
        return '.dll'
    elif is_macos():
        return '.dylib'
    else:
        return '.so'

# Excludes for example android
def native_platform_name() -> str:
    if is_windows():
        return 'windows'
    elif is_linux():
        return 'linux'
    elif is_macos():
        return 'macos'
    else:
        raise Exception("Unsupported platform")

def onerror(func, path, _):
    import stat
    if not os.access(path, os.W_OK):
        os.chmod(path, stat.S_IWUSR)
        func(path)

def rmdir(path: str):
    if os.path.exists(path):
        shutil.rmtree(path, onerror=onerror)