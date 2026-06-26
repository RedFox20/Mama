"""Pins BuildDisplay: TTY live-region rendering + non-TTY fallback, capture/replay, throttle."""
import io, re
from mama.utils.build_display import BuildDisplay, Task

_STRIP = re.compile(r'\x1b\[[0-9;]*[A-Za-z]')  # all ANSI (SGR + cursor), for plain assertions
def strip(s: str) -> str: return _STRIP.sub('', s)


class Clock:
    def __init__(self): self.t = 0.0
    def __call__(self): return self.t
    def tick(self, d=1.0): self.t += d


def _disp(isatty, cols=80, rows=24, **kw):
    out = io.StringIO(); clk = Clock()
    d = BuildDisplay(out, isatty=isatty, term_size=lambda: (cols, rows), clock=clk, color=False, **kw)
    return d, out, clk


def test_non_tty_start_and_finish_lines():
    d, out, clk = _disp(isatty=False)
    d.start_task(1, 'configure', 'foo'); clk.tick(2.0); d.finish_task(1, ok=True)
    text = out.getvalue()
    assert '> configure foo' in text and 'configure' in text and 'foo' in text
    assert '2.0s' in text
    assert '\x1b[' not in text  # never emit ANSI when not a TTY


def test_non_tty_verbose_dumps_full_output():
    d, out, _ = _disp(isatty=False, verbose=True)
    d.start_task(1, 'build', 'bar'); d.feed(1, 'compiling x.cpp'); d.feed(1, 'linking')
    d.finish_task(1, ok=True)
    assert 'compiling x.cpp' in out.getvalue() and 'linking' in out.getvalue()


def test_non_tty_failure_dumps_output_without_verbose():
    d, out, _ = _disp(isatty=False, verbose=False)
    d.start_task(1, 'build', 'bar'); d.feed(1, 'error: boom'); d.finish_task(1, ok=False)
    text = out.getvalue()
    assert 'error: boom' in text          # failed output dumped even without verbose
    assert 'x build' in strip(text)       # fail icon in the summary


def test_tty_region_shows_running_task():
    d, out, clk = _disp(isatty=True)
    d.start_task(1, 'configure', 'foo'); clk.tick(0.2); d.feed(1, 'Checking compiler')
    plain = strip(out.getvalue())
    assert 'configure' in plain and 'foo' in plain and 'Checking compiler' in plain


def test_tty_finish_commits_summary_and_empties_region():
    d, out, clk = _disp(isatty=True)
    d.start_task(1, 'build', 'foo'); clk.tick(3.0); d.finish_task(1, ok=True)
    assert d._drawn == 0  # only task done -> region empty
    assert '\x1b[1A' in out.getvalue()  # region was cleared via cursor-up
    plain = strip(out.getvalue())
    assert 'build' in plain and 'foo' in plain and '3.0s' in plain


def test_tty_caps_region_to_height_with_more_summary():
    d, _, _ = _disp(isatty=True, rows=4)  # cap = rows - margin(1) = 3
    for i in range(5): d.start_task(i, 'build', f't{i}')
    lines = d._region_lines(0.0)
    assert len(lines) == 3 and '+3 more' in strip(lines[-1])


def test_tty_truncates_long_preview_to_width():
    d, _, _ = _disp(isatty=True, cols=20)
    d.start_task(1, 'build', 'x'); d.feed(1, 'y' * 100)
    assert len(strip(d._task_line(d._tasks[1], 0.0, 20))) <= 19


def test_tty_preview_strips_ansi_but_buffer_keeps_it():
    d, _, _ = _disp(isatty=True)
    colored = '\x1b[31mred error\x1b[0m'
    d.start_task(1, 'build', 'x'); d.feed(1, colored)
    line = d._task_line(d._tasks[1], 0.0, 80)
    assert '\x1b[31m' not in line and 'red error' in line
    assert d._tasks[1].lines == [colored]  # raw output preserved for replay


def test_replay_dumps_raw_colored_buffer():
    d, out, _ = _disp(isatty=True)
    d.start_task(1, 'build', 'x'); d.feed(1, '\x1b[31mboom\x1b[0m')
    out.truncate(0); out.seek(0)
    d.replay(1)
    assert '\x1b[31mboom\x1b[0m' in out.getvalue()


def test_render_throttle_skips_within_min_interval():
    d, out, clk = _disp(isatty=True, min_interval=0.1)
    d.start_task(1, 'build', 'x')  # forced render at t=0
    n = len(out.getvalue())
    d.feed(1, 'line2')             # same tick -> throttled, no draw
    assert len(out.getvalue()) == n
    clk.tick(0.2); d.feed(1, 'line3')  # past interval -> draws
    assert len(out.getvalue()) > n


def test_close_clears_region():
    d, _, _ = _disp(isatty=True)
    d.start_task(1, 'build', 'x')
    assert d._drawn == 1
    d.close()
    assert d._drawn == 0


def test_task_feed_tracks_current_and_full_buffer():
    t = Task(1, 'build', 'x', 0.0)
    t.feed('a'); t.feed('   '); t.feed('b')
    assert t.current == 'b'  # blank line did not overwrite the live preview
    assert t.lines == ['a', '   ', 'b']
