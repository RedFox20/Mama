import sys, os, subprocess
from mama.async_file_reader import AsyncFileReader

## Always flush to properly support Jenkins
def console(s): print(s, flush=True)

def execute(command, echo=False):
    if echo: console(command)
    retcode = os.system(command)
    if retcode != 0:
        raise Exception(f'{command} failed with return code {retcode}')

def execute_echo(cwd, cmd):
    proc = subprocess.Popen(cmd, shell=True, cwd=cwd)
    proc.wait()
    if proc.returncode != 0:
        raise Exception(f'Execute {cmd} failed with error: {proc.returncode}')


is_windows = sys.platform == 'win32'
is_linux   = sys.platform.startswith('linux')
is_macos   = sys.platform == 'darwin'
if not (is_windows or is_linux or is_macos):
    raise RuntimeError(f'MamaBuild unsupported platform {sys.platform}')

class System:
    windows = is_windows
    linux   = is_linux
    macos   = is_macos
