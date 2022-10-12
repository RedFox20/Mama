import sys, os, subprocess
from mama.async_file_reader import AsyncFileReader

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


# TODO: use forktty instead of AsyncFileReader
def execute_echo(cwd, cmd):
    try:
        proc = subprocess.Popen(cmd, shell=True, universal_newlines=True, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        output = AsyncFileReader(proc.stdout)
        errors = AsyncFileReader(proc.stderr)
        while True:
            if proc.poll() is None:
                output.print()
                errors.print()
            else:
                output.stop()
                errors.stop()
                output.print()
                errors.print()
                break
    except:
        console(f'Popen failed! cwd={cwd} cmd={cmd} ')
        raise
    if proc.returncode != 0:
        raise Exception(f'Execute {cmd} failed with error: {proc.returncode}')


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
