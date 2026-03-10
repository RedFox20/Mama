import sys, subprocess, platform
from termcolor import colored

is_windows = sys.platform == 'win32'
is_linux   = sys.platform.startswith('linux')
is_macos   = sys.platform == 'darwin'
if not (is_windows or is_linux or is_macos):
    raise RuntimeError(f'MamaBuild unsupported platform {sys.platform}')

machine = platform.machine()
is_aarch64 = machine == 'aarch64' or machine == 'arm64'
is_x86_64 = machine == 'x86_64' or machine == 'AMD64'
is_x86 = machine == 'x86' or machine == 'i386'

class System:
    windows = is_windows
    linux   = is_linux
    macos   = is_macos
    aarch64 = is_aarch64
    x86_64  = is_x86_64
    x86     = is_x86

# Available text colors:
#     black, red, green, yellow, blue, magenta, cyan, white,
#     light_grey, dark_grey, light_red, light_green, light_yellow, light_blue,
#     light_magenta, light_cyan.

# Available text highlights:
#     on_black, on_red, on_green, on_yellow, on_blue, on_magenta, on_cyan, on_white,
#     on_light_grey, on_dark_grey, on_light_red, on_light_green, on_light_yellow,
#     on_light_blue, on_light_magenta, on_light_cyan.
class Color:
    DEFAULT = None
    RED = "red"
    GREEN = "green"
    YELLOW = "yellow"
    BLUE = "blue"
    MAGENTA = "magenta"


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

