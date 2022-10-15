import sys, subprocess


## Always flush to properly support Jenkins
def console(s): print(s, flush=True)


is_windows = sys.platform == 'win32'
is_linux   = sys.platform.startswith('linux')
is_macos   = sys.platform == 'darwin'
if not (is_windows or is_linux or is_macos):
    raise RuntimeError(f'MamaBuild unsupported platform {sys.platform}')


def _is_system_64_bit():
    if sys.platform == 'win32':
        output = subprocess.check_output(['wmic', 'os', 'get', 'OSArchitecture'])
        if '64-bit' in str(output):
            return True
    else:
        output = subprocess.check_output(['uname', '-m'])
        if 'x86_64' in str(output):
            return True
    return False
is_64 = _is_system_64_bit()


class System:
    windows = is_windows
    linux   = is_linux
    macos   = is_macos
    is_64bit = is_64
