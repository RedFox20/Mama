import sys, subprocess, platform, threading, contextlib
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


# Serialize writes and finalize any pending progress line before a normal
# status print, so parallel redraws don't get glued to status lines.
_console_lock = threading.Lock()
_progress_active = False  # last write left cursor mid-row
_ERASE_EOL = '\x1b[K'  # ANSI erase-to-end-of-line (colorama enables it on Windows)
_active_display = None  # duck-typed BuildDisplay; routes normal lines above its live region
_capture = threading.local()  # per-thread sink: a running job's console() lines go to its display task


def set_active_display(display):
    """While a live display is active, normal console() lines route above its region instead of
    tearing it. None detaches. Duck-typed (has print_above) to avoid importing build_display."""
    global _active_display
    _active_display = display


def capture_context():
    """Snapshot this thread's console-capture state as a (sink, display, tid, build_slot) tuple. A
    helper thread that runs io_func - SubProcess's reader thread - re-establishes it via
    capture_to(*ctx); without that, io_func's console() lines have no sink and leak above the live
    region instead of feeding the owning display task."""
    return (getattr(_capture, 'sink', None), getattr(_capture, 'display', None),
            getattr(_capture, 'tid', None), getattr(_capture, 'build_slot', None))


@contextlib.contextmanager
def capture_to(sink, display=None, tid=None, build_slot=None):
    """Route THIS thread's console() lines to `sink` (a display task feed) so a job's banners land
    in its display line instead of tearing the live region; restores the previous sink on exit.
    `display`/`tid` let SubProcess report child pids for CPU sampling; `build_slot` is the
    scheduler barrier so a custom build()'s cmake_build() can self-gate."""
    prev = capture_context()
    _capture.sink, _capture.display, _capture.tid, _capture.build_slot = sink, display, tid, build_slot
    try:
        yield
    finally:
        _capture.sink, _capture.display, _capture.tid, _capture.build_slot = prev


def build_barrier(weight: int):
    """Wrap a heavy compile (cmake_build's build step) so it occupies `weight` budget cores in the
    active scheduler, suspending the worker until admitted. A no-op (null context) on the serial
    path / in tests, so mamafile build() call sites need no changes."""
    factory = getattr(_capture, 'build_slot', None)
    return factory(weight) if factory is not None else contextlib.nullcontext()


def report_subprocess(pid: int, started: bool):
    """SubProcess calls this on child start/exit, routing the pid to this thread's display task
    (set by capture_to) for process-tree CPU sampling. Best-effort: never breaks a build."""
    display = getattr(_capture, 'display', None)
    tid = getattr(_capture, 'tid', None)
    if display is None or tid is None: return
    try:
        if started: display.attach_pid(tid, pid)
        else:       display.detach_pid(tid, pid)
    except Exception:
        pass


def console(text:str, color=None, end="\n"):
    """ Always flush to support most build environments """
    global _progress_active
    is_redraw = text.startswith('\r')        # redraws start with \r (cursor reset); see progress()
    clean = text[1:] if is_redraw else text  # \r stripped: line-based sinks/region want a clean line
    # While a display owns the screen, route EVERYTHING through it - any direct stdout write (even a
    # \r-redraw or a partial) desyncs the region's cursor math and walks it down the screen. Owned
    # output feeds the job's task preview; an ownerless full line goes above the region; an ownerless
    # mid-progress redraw is dropped (can't place it in the line-based region without corrupting it).
    sink = getattr(_capture, 'sink', None)
    if sink is not None or _active_display is not None:
        colored = get_colored_text(clean, color)
        if sink is not None: sink(colored)
        elif end == '\n': _active_display.print_above(colored)
        return
    text = get_colored_text(text, color)
    with _console_lock:
        # a status line right after an in-flight \r-progress needs a leading \n so it isn't overwritten
        if _progress_active and not is_redraw:
            print()
        if is_redraw: text += _ERASE_EOL  # erase-to-EOL so a shorter redraw clears the longer prev line
        print(text, end=end, flush=True)
        _progress_active = (end != '\n')


def progress(text:str, color=None, final=False):
    """Redraw an in-place progress line, always cleared to end-of-line. `final=True`
    commits it with a newline; otherwise the cursor stays put for the next redraw."""
    console('\r' + text, color=color, end='\n' if final else '')


def error(text:str):
    """ Prints a message as an error, usually colored red """
    console(text, color=Color.RED)


def warning(text:str):
    """ Prints a message as a warning, colored yellow """
    console(text, color=Color.YELLOW)

