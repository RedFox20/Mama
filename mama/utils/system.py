import sys, os, subprocess
from .sub_process import SubProcess


## Always flush to properly support Jenkins
def console(s): print(s, flush=True)


def execute(command, echo=False, throw=True):
    if echo: console(command)
    retcode = os.system(command)
    if throw and retcode != 0:
        raise Exception(f'{command} failed with return code {retcode}')
    return retcode


def execute_piped(command, cwd=None):
    cp = subprocess.run(command, stdout=subprocess.PIPE, cwd=cwd)
    return cp.stdout.decode('utf-8').rstrip()


def execute_echo(cwd, cmd):
    exit_status = -1
    try:
        exit_status = SubProcess.run(cmd, cwd)
    except:
        console(f'SubProcess failed! cwd={cwd} cmd={cmd} ')
        raise
    if exit_status != 0:
        raise Exception(f'Execute {cmd} failed with error: {exit_status}')


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
