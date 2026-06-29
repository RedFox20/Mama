"""Pins BuildDisplay: TTY live-region rendering + non-TTY fallback, capture/replay, throttle."""
import io, re
from types import SimpleNamespace
from mama.utils import system
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


def test_non_tty_emits_one_summary_line_per_slow_task():
    d, out, clk = _disp(isatty=False)
    d.start_task(1, 'configure', 'foo'); clk.tick(2.0); d.finish_task(1, ok=True)
    text = out.getvalue()
    assert 'configure' in text and 'foo' in text and '2.0s' in text  # one finish summary, no start line
    assert '\x1b[' not in text  # never emit ANSI when not a TTY


def test_elapsed_over_a_minute_uses_the_shared_mm_ss_formatter():
    d, out, clk = _disp(isatty=False)
    d.start_task(1, 'build', 'krattgcs', detail='J32'); clk.tick(164.3); d.finish_task(1, ok=True)
    text = out.getvalue()
    assert '2m 44s' in text and '164' not in text  # get_time_str, not raw 164.3s


def test_instant_success_tasks_are_hidden_failures_are_not():
    d, out, clk = _disp(isatty=False)
    d.start_task(1, 'build', 'instant'); d.finish_task(1, ok=True)        # ~0.0s success -> hidden
    d.start_task(2, 'build', 'slow'); clk.tick(0.5); d.finish_task(2, ok=True)   # slow -> shown
    d.start_task(3, 'build', 'boom'); d.finish_task(3, ok=False)          # instant FAIL -> still shown
    text = out.getvalue()
    assert 'instant' not in text and 'slow' in text and 'boom' in text


def test_phases_merge_into_one_summary_with_breakdown():
    d, out, clk = _disp(isatty=False)
    d.start_task('geo', 'pulling', 'geo'); clk.tick(3.7); d.finish_task('geo', ok=True, final=False)
    assert out.getvalue() == ''   # an intermediate phase commits nothing - the dep is still working
    d.start_task('geo', 'configure', 'geo'); clk.tick(0.3); d.finish_task('geo', ok=True, final=False)
    d.start_task('geo', 'build', 'geo', detail='J32'); clk.tick(0.5); d.finish_task('geo', ok=True, final=True)
    text = strip(out.getvalue())
    assert text.count('geo') == 1 and 'build J32' in text  # one merged line; kind = last phase that did work
    assert 'Git 3.7s' in text and 'Cfg 0.3s' in text and 'Bld 0.5s' in text  # git pull, configure, build


def test_lone_phase_still_shows_its_tag():
    d, out, clk = _disp(isatty=False)
    d.start_task('x', 'build', 'x', detail='J4'); clk.tick(2.0); d.finish_task('x', ok=True)  # build-only dep
    assert 'Bld 2.0s' in out.getvalue()  # the tag shows even for a lone phase, for a consistent column


def test_instant_phase_dropped_and_live_line_shows_prior_phases():
    d, _, clk = _disp(isatty=True)
    d.start_task('g', 'pulling', 'g'); clk.tick(3.7); d.finish_task('g', ok=True, final=False)  # 3.7s pull recorded
    d.start_task('g', 'configure', 'g'); d.finish_task('g', ok=True, final=False)  # instant configure -> dropped
    d.start_task('g', 'build', 'g', detail='J8'); clk.tick(0.5)                    # now building
    line = strip(d._task_line(d._tasks['g'], clk(), 120))
    assert 'Git 3.7s' in line and 'Bld 0.5s' in line and 'Cfg' not in line  # prior pull shown; instant cfg absent


def test_phase_tags_collapse_git_loads_and_label_each_source():
    tag = BuildDisplay._tag
    assert tag('check') == tag('clone') == tag('pulling') == 'Git'  # all git loads share one tag
    assert tag('local') == 'Loc' and tag('artifactory') == 'Art'
    assert tag('configure') == 'Cfg' and tag('build') == 'Bld'


def test_non_tty_verbose_dumps_full_output():
    d, out, clk = _disp(isatty=False, verbose=True)
    d.start_task(1, 'build', 'bar'); d.feed(1, 'compiling x.cpp'); d.feed(1, 'linking')
    clk.tick(0.2); d.finish_task(1, ok=True)   # past reveal: a real build that emitted output isn't instant
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
    d.start_task(1, 'build', 'foo'); clk.tick(0.5); d.render(force=True)  # draw the live line first
    assert d._drawn == 1
    clk.tick(2.5); d.finish_task(1, ok=True)  # total 3.0s
    assert d._drawn == 0  # only task done -> region empty
    assert '\x1b[1A' in out.getvalue()  # the drawn line was cleared via cursor-up
    plain = strip(out.getvalue())
    assert 'build' in plain and 'foo' in plain and '3.0s' in plain


def test_tty_caps_region_to_height_with_more_summary():
    d, _, _ = _disp(isatty=True, rows=4)  # cap = rows - margin(1) = 3
    for i in range(5): d.start_task(i, 'build', f't{i}')
    lines = d._region_lines(1.0)  # now=1.0 so all 5 are past the reveal delay
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


def test_build_detail_shows_core_count_after_kind():
    d, _, clk = _disp(isatty=True)
    d.start_task(1, 'build', 'compression', detail='J16'); clk.tick(0.5)
    assert 'build J16' in strip(d._task_line(d._tasks[1], clk(), 80))
    d.finish_task(1, ok=True)
    assert 'build J16' in strip(d._summary_line(d._tasks[1]))


def test_cpu_sampling_updates_task_and_renders_percent():
    d, _, clk = _disp(isatty=True, cpu_sampler=lambda snap: {t: 597.0 for t in snap}, sample_interval=999)
    d.start_task(1, 'build', 'compression', detail='J16')
    d.attach_pid(1, 4242)
    d._sample_once()
    assert d._tasks[1].cpu == 597.0
    assert 'build J16 cpu:597%' in strip(d._task_line(d._tasks[1], clk(), 120))
    d.detach_pid(1, 4242)
    assert d._tasks[1].cpu == 0.0  # subprocess gone -> CPU cleared, not left stale
    d.close()


def test_late_cpu_sample_does_not_resurrect_a_detached_task():
    # The sampler runs off-lock; if the task detaches during that window, the stale CPU it computed
    # must NOT be written back - else a dead subprocess shows as busy until the task finishes.
    d, _, _ = _disp(isatty=True, sample_interval=999)
    d.start_task(1, 'build', 'x'); d.attach_pid(1, 7)
    def sampler(snap):
        d.detach_pid(1, 7)   # subprocess exits mid-scan: tid dropped, cpu zeroed
        return {1: 888.0}    # a stale reading computed just before the detach
    d._cpu_sampler = sampler
    d._sample_once()
    assert d._tasks[1].cpu == 0.0   # stale 888% dropped, not resurrected
    d.close()


def test_sampler_backoff_caps_cost_at_tenth_of_walltime():
    d, _, _ = _disp(isatty=True, sample_interval=1.5)
    assert d._next_wait(0.01) == 1.5      # cheap sample -> base interval
    assert d._next_wait(5.7) == 5.7 * 9   # a 5.7s sample -> wait ~51s, so it stays ~10% of wall-time
    d.close()


def test_report_subprocess_attaches_pid_to_current_task():
    d, _, _ = _disp(isatty=True, cpu_sampler=lambda snap: {t: 100.0 for t in snap}, sample_interval=999)
    tid = ('x', 'build'); d.start_task(tid, 'build', 'x')
    with system.capture_to(lambda line: None, d, tid):
        system.report_subprocess(999, True)
        assert d._pids[tid] == {999}
        system.report_subprocess(999, False)
        assert tid not in d._pids
    d.close()


def test_attach_pid_only_samples_build_tasks():
    d, _, _ = _disp(isatty=True, cpu_sampler=lambda snap: {}, sample_interval=999)
    d.start_task(('c', 'configure'), 'configure', 'c'); d.attach_pid(('c', 'configure'), 11)
    d.start_task(('l', 'load'), 'clone', 'l'); d.attach_pid(('l', 'load'), 12)
    d.start_task(('b', 'build'), 'build', 'b'); d.attach_pid(('b', 'build'), 13)
    assert d._pids == {('b', 'build'): {13}}   # configure/clone not sampled, only build
    d.close()


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


def test_set_pending_shows_then_clears_the_blocked_task_line():
    d, _, _ = _disp(isatty=True, rows=24)
    d.start_task(1, 'build', 'krattlink', detail='J12')
    d.set_pending(('geo', 'cpu 92% >= 85%'))
    region = strip('\n'.join(d._region_lines(1.0)))
    assert 'pending' in region and 'geo' in region and 'cpu 92%' in region
    d.set_pending(None)
    assert 'pending' not in strip('\n'.join(d._region_lines(1.0)))
    d.close()


def test_render_skips_when_another_thread_is_drawing():
    # A non-forced render must not block while another thread holds the render lock (that block would
    # stall the subprocess reader -> fill the pipe -> stall the compiler). It skips; a later draw covers it.
    d, out, clk = _disp(isatty=True)
    d.start_task(1, 'build', 'x'); clk.tick(0.2)
    d._render_lock.acquire()
    try:
        before = len(out.getvalue())
        d.render()                              # busy -> skip, no draw, no block
        assert len(out.getvalue()) == before
    finally:
        d._render_lock.release()
    d.render(force=True)                         # free -> draws the running task
    assert len(out.getvalue()) > before


def test_close_clears_region():
    d, _, clk = _disp(isatty=True)
    d.start_task(1, 'build', 'x'); clk.tick(0.2); d.render(force=True)  # past reveal -> 1 line drawn
    assert d._drawn == 1
    d.close()
    assert d._drawn == 0


def test_build_barrier_is_noop_without_scheduler_else_uses_slot():
    import contextlib
    with system.build_barrier(8):                       # no active scheduler -> null context, never blocks
        pass
    calls = []
    slot = lambda w: contextlib.nullcontext(calls.append(w))   # records the requested weight
    with system.capture_to(lambda l: None, build_slot=slot):
        with system.build_barrier(5):
            pass
    assert calls == [5]                                  # routed the compile's weight to the scheduler slot


def test_capture_to_routes_console_to_sink_and_restores():
    outer, inner = [], []
    with system.capture_to(outer.append):
        system.console('a')
        with system.capture_to(inner.append):   # nested job sink
            system.console('b')
        system.console('c')                      # back to the outer sink after the nested block
    assert 'a' in outer[0] and 'c' in outer[1] and inner == ['b']
    assert getattr(system._capture, 'sink', None) is None  # fully restored


def test_progress_redraw_feeds_sink_as_clean_line(capsys):
    sink = []
    with system.capture_to(sink.append):
        system.progress('cloning 42%')          # \r-progress while a job owns the screen
    assert sink == ['cloning 42%']               # clean preview line: \r stripped, nothing to stdout
    assert capsys.readouterr().out == ''


def test_active_display_routes_status_above_region_and_drops_bare_redraw(capsys):
    calls = []
    system.set_active_display(SimpleNamespace(print_above=calls.append))
    try:
        system.console('status line')           # ownerless line-complete -> above the region
        system.progress('downloading 10%')      # ownerless mid-progress redraw -> dropped, not stdout
        system.progress('done', final=True)     # final commit -> above the region, \r stripped
    finally:
        system.set_active_display(None)
    assert calls == ['status line', 'done']
    assert capsys.readouterr().out == ''         # display owns the screen: no direct stdout writes


def test_task_feed_tracks_current_and_full_buffer():
    t = Task(1, 'build', 'x', 0.0)
    t.feed('a'); t.feed('   '); t.feed('b')
    assert t.current == 'b'  # blank line did not overwrite the live preview
    assert t.lines == ['a', '   ', 'b']
