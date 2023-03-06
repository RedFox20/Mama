import sys, subprocess
from termcolor import colored

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


class Color:
    DEFAULT = None
    RED = "red"
    GREEN = "green"
    YELLOW = "yellow"
    BLUE = "blue"


# on windows use colorama to enable ANSI color escape sequences
if System.windows:
    from colorama import just_fix_windows_console
    just_fix_windows_console()


def get_colored_text(text:str, color):
    return colored(text, color=color) if color else text


def console(text:str, color=None, end="\n"):
    """ Always flush to support most build environments """
    print(get_colored_text(text, color), end=end, flush=True)


def error(text:str):
    """ Prints a message as an error, usually colored red """
    console(text, color=Color.RED)

