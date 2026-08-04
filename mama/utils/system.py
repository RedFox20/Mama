import os, sys, threading, contextlib, time
from termcolor import colored

is_windows = sys.platform == 'win32'
is_linux   = sys.platform.startswith('linux')
is_macos   = sys.platform == 'darwin'
if not (is_windows or is_linux or is_macos):
    raise RuntimeError(f'MamaBuild unsupported platform {sys.platform}')


def _machine_name() -> str:
    """The cpu architecture of this host, in the spelling that host reports.

    NOT platform.machine(). Python 3.12 and later answer platform.uname() on Windows with a WMI
    query, which costs about 60ms of EVERY mama start. The environment and os.uname() hold the
    same fact for free. platform.machine() stays as the last resort, so no host loses detection,
    and its import stays inside this branch because the import alone costs about 12ms."""
    if is_windows:
        # A 32-bit python on 64-bit windows reads x86 from PROCESSOR_ARCHITECTURE, and the real
        # architecture from PROCESSOR_ARCHITEW6432.
        name = os.environ.get('PROCESSOR_ARCHITEW6432') or os.environ.get('PROCESSOR_ARCHITECTURE', '')
    else:
        name = os.uname().machine if hasattr(os, 'uname') else ''
    if name: return name
    import platform
    return platform.machine()


machine = _machine_name()
# Compare without case. Windows spells the same architecture ARM64 and AMD64, and a case-keeping
# compare read Windows-on-ARM as neither aarch64 nor x86.
_arch = machine.lower()
is_aarch64 = _arch == 'aarch64' or _arch == 'arm64'
is_x86_64 = _arch == 'x86_64' or _arch == 'amd64'
is_x86 = _arch == 'x86' or _arch == 'i386'

class System:
    windows = is_windows
    linux   = is_linux
    macos   = is_macos
    aarch64 = is_aarch64
    x86_64  = is_x86_64
    x86     = is_x86

# Values are termcolor color names. Extend from the termcolor palette when a new color is needed.
class Color:
    DEFAULT = None
    RED = "red"
    GREEN = "green"
    YELLOW = "yellow"
    BLUE = "blue"
    MAGENTA = "magenta"


# on Windows, colorama enables ANSI color escape sequences
if System.windows:
    from colorama import just_fix_windows_console
    just_fix_windows_console()


def get_colored_text(text:str, color):
    return colored(text, color=color) if color else text


# serialize writes and finalize a pending progress line before a status print, so redraws do not glue to status lines
_console_lock = threading.Lock()
_progress_active = False  # last write left cursor mid-row
_ERASE_EOL = '\x1b[K'  # ANSI erase-to-end-of-line (colorama enables it on Windows)
_active_display = None  # duck-typed BuildDisplay, routes normal lines above its live region
_capture = threading.local()  # per-thread sink: a running job's console() lines go to its display task

# Set by GitHub Actions and GitLab CI (CI), Azure Pipelines, Jenkins and TeamCity.
_CI_ENV_VARS = ('CI', 'TF_BUILD', 'JENKINS_URL', 'TEAMCITY_VERSION')
_HEADLESS_PROGRESS_INTERVAL = 5.0  # seconds between progress redraws when nothing can redraw in place
_headless = None  # tri-state cache for is_headless()
_progress_at = threading.local()  # per-thread time of the last headless progress redraw


def is_headless() -> bool:
    """True when no terminal redraws a progress bar in place: a CI runner, or a piped or redirected
    stdout. Decided ONCE - the environment and the output stream are constant for the whole process."""
    global _headless
    if _headless is None:
        isatty = getattr(sys.stdout, 'isatty', lambda: False)  # a redirected stdout may not have it
        _headless = any(os.environ.get(v) for v in _CI_ENV_VARS) or not isatty()
    return _headless


def _redraw_due() -> bool:
    """True when a progress redraw may print. Without a terminal a redraw appends a line instead of
    overwriting one, so those throttle to one line per _HEADLESS_PROGRESS_INTERVAL per thread. The first
    call only starts the timer, so a transfer that finishes inside one interval prints only its final
    line. A captured redraw is exempt: it feeds the live display, which decides what reaches the screen."""
    if not is_headless() or getattr(_capture, 'sink', None) is not None: return True
    now = time.monotonic()
    last = getattr(_progress_at, 'at', None)
    due = last is not None and (now - last) >= _HEADLESS_PROGRESS_INTERVAL
    if due or last is None: _progress_at.at = now
    return due


def set_active_display(display):
    """While a live display is active, normal console() lines route above its region instead of
    tearing it. None detaches. Duck-typed (has print_above) to avoid importing build_display."""
    global _active_display
    _active_display = display


def capture_context():
    """Snapshot this thread's console-capture state as a (sink, display, tid, build_slot) tuple. A helper
    thread that runs io_func restores it with capture_to(*ctx). Without that, io_func's console() lines
    have no sink and leak above the live region instead of feeding the owning display task."""
    return (getattr(_capture, 'sink', None), getattr(_capture, 'display', None),
            getattr(_capture, 'tid', None), getattr(_capture, 'build_slot', None))


@contextlib.contextmanager
def capture_to(sink, display=None, tid=None, build_slot=None):
    """Route THIS thread's console() lines to `sink` for the `with` block, restoring the previous sink on exit.
    sink: a display task feed, so a job's banners land in its display line instead of tearing the live region
    display, tid: let SubProcess report child pids for CPU sampling
    build_slot: the scheduler barrier, so a custom build()'s cmake_build() can self-gate"""
    prev = capture_context()
    _capture.sink, _capture.display, _capture.tid, _capture.build_slot = sink, display, tid, build_slot
    try:
        yield
    finally:
        _capture.sink, _capture.display, _capture.tid, _capture.build_slot = prev


def build_barrier(weight: int):
    """Wrap a heavy compile so it occupies `weight` budget cores in the active scheduler, suspending the
    worker until admitted. A no-op on the serial path and in tests, so mamafile call sites need no changes.
    weight: the number of budget cores the compile occupies"""
    factory = getattr(_capture, 'build_slot', None)
    return factory(weight) if factory is not None else contextlib.nullcontext()


def report_subprocess(pid: int, started: bool):
    """SubProcess calls this on child start/exit, routing the pid to this thread's display task (set by
    capture_to) for process-tree CPU sampling. Best-effort: never breaks a build."""
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
    is_redraw = text.startswith('\r')        # redraws start with \r (cursor reset), see progress()
    clean = text[1:] if is_redraw else text  # \r stripped: line-based sinks/region want a clean line
    # While a display owns the screen, route EVERYTHING through it: any direct stdout write desyncs the
    # region's cursor math. Owned output feeds the job's task preview, an ownerless full line goes above
    # the region, and an ownerless mid-progress redraw is dropped: the region cannot place it.
    sink = getattr(_capture, 'sink', None)
    if sink is not None or _active_display is not None:
        # Split an embedded-newline message into SEPARATE lines: the display is line-based, and a multi-line
        # string smuggled through as one 'line' shifts the cursor and strands task lines in the scrollback.
        for part in (clean.split('\n') if '\n' in clean else (clean,)):
            line = get_colored_text(part, color)  # not `colored`: that name is termcolor's, imported above
            if sink is not None: sink(line)
            elif end == '\n': _active_display.print_above(line)
        return
    text = get_colored_text(text, color)
    with _console_lock:
        # a status line right after an in-flight \r-progress needs a leading \n so the redraw does not overwrite it
        if _progress_active and not is_redraw:
            print()
        if is_redraw: text += _ERASE_EOL  # erase-to-EOL so a shorter redraw clears the longer prev line
        print(text, end=end, flush=True)
        _progress_active = (end != '\n')


def progress(text:str, color=None, final=False):
    """Redraw an in-place progress line, always cleared to end-of-line. `final=True` commits it with a
    newline and always prints, else the cursor stays for the next redraw (throttled, see _redraw_due)."""
    if not final and not _redraw_due(): return
    console('\r' + text, color=color, end='\n' if final else '')


def error(text:str):
    """ Prints an error message, colored red """
    console(text, color=Color.RED)


def warning(text:str):
    """ Prints a warning message, colored yellow """
    console(text, color=Color.YELLOW)


def warning_to(out, text:str):
    """Send a warning to a target's output sink, or print it when there is none. The sink feeds the
    per-target display block and mamabuild.log, which a plain print never reaches."""
    if out: out(get_colored_text(text, Color.YELLOW))
    else: warning(text)

