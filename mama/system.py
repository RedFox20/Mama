import sys, os

## Always flush to properly support Jenkins
def console(s): print(s, flush=True)

def execute(command):
    retcode = os.system(command)
    if retcode != 0:
        raise Exception(f'{command} failed with return code {retcode}')

is_windows = sys.platform == 'win32'
is_linux   = sys.platform.startswith('linux')
is_macos   = sys.platform == 'darwin'
if not (is_windows or is_linux or is_macos):
    raise RuntimeError(f'MamaBuild unsupported platform {sys.platform}')

class System:
    windows = is_windows
    linux   = is_linux
    macos   = is_macos
