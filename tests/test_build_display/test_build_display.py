"""Pins BuildDisplay: TTY live-region rendering + non-TTY fallback, capture/replay, throttle."""
import io, re
from types import SimpleNamespace
from mama.utils import system
from mama.utils.build_display import BuildDisplay, Task, _fmt_dur, scan_diagnostics
from testutils import strip_ansi as strip

def squeeze(s: str) -> str: return re.sub(r' +', ' ', strip(s))  # collapse fixed-width padding for breakdown asserts


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
    d.start_task(1, 'build', 'myapp', detail='J32'); clk.tick(164.3); d.finish_task(1, ok=True)
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
    text = squeeze(out.getvalue())
    assert text.count('geo') == 1 and 'build J32' in text  # one merged line, kind = last phase that did work
    assert 'git 3.7s' in text and 'cfg 0.3s' in text and 'bld 0.5s' in text  # git pull, configure, build


def test_summary_keeps_an_instant_phase_when_the_dep_did_real_work():
    d, out, clk = _disp(isatty=False)
    d.start_task('z', 'pulling', 'z'); clk.tick(2.0); d.finish_task('z', ok=True, final=False)  # real load
    d.start_task('z', 'configure', 'z'); d.finish_task('z', ok=True, final=False)  # instant configure
    d.start_task('z', 'build', 'z', detail='J4'); clk.tick(0.5); d.finish_task('z', ok=True, final=True)
    assert 'cfg 0.0s' in squeeze(out.getvalue())  # the instant configure is shown, not hidden - "did cfg run?"


def test_subsecond_phase_shows_fractional_seconds_not_milliseconds():
    # ms is noise in the breakdown: a 34ms step reads '.03s'. (>=0.1s keeps get_time_str: 'git 2.0s'.)
    d, out, clk = _disp(isatty=False)
    d.start_task('m', 'pulling', 'm'); clk.tick(2.0); d.finish_task('m', ok=True, final=False)
    d.start_task('m', 'build', 'm', detail='J4'); clk.tick(0.034); d.finish_task('m', ok=True, final=True)
    text = squeeze(out.getvalue())
    assert 'git 2.0s' in text and 'bld 0.03s' in text  # 34ms shown as 0.03s, no ms noise


def test_instant_phase_rounds_to_one_decimal_not_two_zeros():
    assert _fmt_dur(0.0).strip() == '0.0s' and _fmt_dur(0.002).strip() == '0.0s'  # not an over-precise 0.00s
    assert _fmt_dur(0.01).strip() == '0.01s'   # a real sub-0.1s value keeps 2 decimals


def test_lone_phase_still_shows_its_tag():
    d, out, clk = _disp(isatty=False)
    d.start_task('x', 'build', 'x', detail='J4'); clk.tick(2.0); d.finish_task('x', ok=True)  # build-only dep
    assert 'bld 2.0s' in squeeze(out.getvalue())  # the tag shows even for a lone phase, for a consistent column


def test_live_line_shows_all_prior_phases_including_an_instant_one():
    d, _, clk = _disp(isatty=True)
    d.start_task('g', 'pulling', 'g'); clk.tick(3.7); d.finish_task('g', ok=True, final=False)  # 3.7s pull
    d.start_task('g', 'configure', 'g'); d.finish_task('g', ok=True, final=False)  # instant configure
    d.start_task('g', 'build', 'g', detail='J8'); clk.tick(0.5)                    # now building
    line = squeeze(d._task_line(d._tasks['g'], clk(), 120))
    assert 'git 3.7s' in line and 'cfg 0.0s' in line and 'bld 0.5s' in line  # every step shown, even the instant cfg


def test_note_names_the_package_on_the_summary_line():
    d, out, clk = _disp(isatty=False)
    d.start_task('gt', 'artifactory', 'googletest'); d.set_note('gt', 'googletest-ubuntu-24-x64-release-ae51a95')
    clk.tick(0.7); d.finish_task('gt', ok=True)
    assert 'art 0.7s googletest-ubuntu-24-x64-release-ae51a95' in squeeze(out.getvalue())


def test_an_empty_note_keeps_the_summary_line_bare():
    d, out, clk = _disp(isatty=False)
    d.start_task('s', 'clone', 'SDL'); d.set_note('s', ''); clk.tick(2.9); d.finish_task('s', ok=True)
    assert squeeze(out.getvalue()).rstrip().endswith('git 2.9s')


def test_phase_tags_collapse_git_loads_and_label_each_source():
    tag = BuildDisplay._tag
    assert tag('check') == tag('clone') == tag('pulling') == 'git'  # all git loads share one tag
    assert tag('local') == 'loc' and tag('artifactory') == 'art'
    assert tag('configure') == 'cfg' and tag('build') == 'bld'


def test_non_tty_verbose_dumps_full_output():
    d, out, clk = _disp(isatty=False, verbose=True)
    d.start_task(1, 'build', 'bar'); d.feed(1, 'compiling x.cpp'); d.feed(1, 'linking')
    clk.tick(0.2); d.finish_task(1, ok=True)   # past reveal: a real build that emitted output is not instant
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


def test_region_line_drops_cursor_moves_smuggled_in_by_a_nested_display():
    """A nested build's own live region (a `mama` host-binary bootstrap, ninja) arrives as preview
    text. One cursor-up reaching the terminal strands every finished task line in the scrollback."""
    d, _, clk = _disp(isatty=True)
    d.start_task(1, 'build', 'protobuf'); clk.tick(0.5)
    d.feed(1, '\x1b[1A\x1b[2Knested frame')
    line = d._task_line(d._tasks[1], clk(), 80)
    assert '\x1b[' not in line and 'nested frame' in line


def test_region_line_drops_tabs_that_widen_it_past_the_terminal():
    """A tab counts as one character but renders up to eight columns, so the line wrapped and desynced."""
    d, _, clk = _disp(isatty=True)
    d.start_task(1, 'build', 'libffmpeg'); clk.tick(0.5)
    d.feed(1, 'X86ASM\tlibavfilter/x86/af_volume.o')
    line = d._task_line(d._tasks[1], clk(), 120)
    assert '\t' not in line and 'af_volume.o' in line


def test_truncate_keeps_colour_while_dropping_cursor_moves():
    d, _, _ = _disp(isatty=True)
    assert d._truncate('\x1b[32m+\x1b[0m \x1b[1Aok', 80) == '\x1b[32m+\x1b[0m ok'


def test_platform_tags_every_status_line():
    d, _, clk = _disp(isatty=True, platform='android')
    d.start_task(1, 'build', 'protobuf'); clk.tick(2.0)
    assert '[android] bld' in strip(d._task_line(d._tasks[1], clk(), 80))
    d.finish_task(1, ok=True)
    assert '[android] bld' in strip(d._summary_line(d._tasks[1]))


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
    # The sampler runs off-lock: a stale CPU written after a mid-scan detach shows a dead subprocess as busy until finish.
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
    d.start_task(1, 'build', 'netlib', detail='J12')
    d.set_pending(('geo', 'cpu 92% >= 85%'))
    region = strip('\n'.join(d._region_lines(1.0)))
    assert 'pending' in region and 'geo' in region and 'cpu 92%' in region
    d.set_pending(None)
    assert 'pending' not in strip('\n'.join(d._region_lines(1.0)))
    d.close()


def test_render_skips_when_another_thread_is_drawing():
    # A blocked non-forced render stalls the reader -> fills the pipe -> stalls the compiler. Skip, a later draw covers it.
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


def test_multiline_console_reaches_the_sink_as_separate_lines():
    # A multi-line message passed as ONE line shifts the cursor and desyncs the live region's cursor-up math.
    sink = []
    with system.capture_to(sink.append):
        system.console('\n\n#### CMakeBuild myapp')
    assert sink == ['', '', '#### CMakeBuild myapp']
    assert not any('\n' in line for line in sink)


def test_multiline_console_reaches_print_above_as_separate_lines():
    lines = []
    system.set_active_display(SimpleNamespace(print_above=lines.append))
    try:
        system.console('first\nsecond')
    finally:
        system.set_active_display(None)
    assert lines == ['first', 'second']


def test_region_line_never_contains_a_newline():
    d, _, clk = _disp(isatty=True)
    d.start_task(1, 'build', 'x'); clk.tick(0.5)
    d.feed(1, 'preview with\nan embedded newline')   # even if one slips in, the region must stay 1 line
    line = d._task_line(d._tasks[1], clk(), 200)
    assert '\n' not in line and '\r' not in line


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


def test_task_feed_collapses_git_progress_flood():
    t = Task(1, 'build', 'x', 0.0)
    t.feed('remote: Counting objects:   0% (1/290)')
    t.feed('remote: Counting objects:  50% (145/290)')
    t.feed('remote: Counting objects: 100% (290/290), done.')
    t.feed('linking x')  # a real line ends the progress run and is appended
    assert t.lines == ['remote: Counting objects: 100% (290/290), done.', 'linking x']
    assert t.current == 'linking x'


def test_task_feed_collapses_download_progress_bar():
    t = Task(1, 'build', 'x', 0.0)
    for p in (0, 25, 50, 75, 100):  # a custom build's own downloader, per-percent frames
        t.feed(f'x264  {p}% [{"="*(p//10)}>] 3.4MB/s')
    t.feed('unpacking x264')
    assert t.lines == ['x264  100% [==========>] 3.4MB/s', 'unpacking x264']  # collapsed to the final frame


def test_scan_diagnostics_picks_msvc_and_gcc_errors_first_and_dedups():
    diags, n_err, n_warn = scan_diagnostics([
        'foo.cpp(12): warning C4996: deprecated',
        "bar.cpp:5:3: error: expected ';'",
        '\x1b[31mbaz.cpp:1:1: warning: unused variable\x1b[0m',   # ansi stripped before matching
        'foo.cpp(12): warning C4996: deprecated',                # duplicate -> collapsed
        'obj.obj : error LNK2019: unresolved external', 'linking...', 'note: in expansion'])
    assert n_err == 2 and n_warn == 2
    assert diags[0][0] == 'error' and diags[1][0] == 'error'                    # errors before warnings
    assert ('warning', 'baz.cpp:1:1: warning: unused variable') in diags        # ansi stripped in the stored text


def test_scan_diagnostics_caps_and_counts_full_totals():
    diags, n_err, n_warn = scan_diagnostics([f'f{i}.cpp:1:1: warning: w{i}' for i in range(12)], limit=8)
    assert len(diags) == 8 and n_warn == 12 and n_err == 0   # capped list, but the count reflects all 12


def test_scan_diagnostics_catches_cmakes_error_at_form():
    # 'CMake Error at <file>' has no colon after the severity, so the generic form alone misses it.
    diags, n_err, n_warn = scan_diagnostics([
        'CMake Error at /usr/share/cmake-3.28/Modules/FindOpenSSL.cmake:668 (find_package):',
        'CMake Warning at CMakeLists.txt:5 (message):'])
    assert n_err == 1 and n_warn == 1
    assert diags[0][0] == 'error' and 'FindOpenSSL' in diags[0][1]


def test_scan_diagnostics_ignores_non_diagnostic_error_words():
    diags, n_err, n_warn = scan_diagnostics(['-Werror is set', '0 errors, 0 warnings', 'std::error_code x;'])
    assert diags == [] and n_err == 0 and n_warn == 0


def test_diagnostics_reads_a_finished_task_buffer():
    d, _, _ = _disp(isatty=False)
    d.start_task('t', 'build', 't'); d.feed('t', 'a.cpp:1:1: warning: oops'); d.finish_task('t', ok=True)
    assert d.diagnostics('t') == ([('warning', 'a.cpp:1:1: warning: oops')], 0, 1)


class _Log:
    def __init__(self): self.text = ''
    def write(self, s): self.text += s
    def close(self): pass


def test_log_writes_full_per_target_block_even_when_hidden():
    log = _Log()
    d, _, _ = _disp(isatty=False, log=log)  # instant build -> hidden from the display, but still logged
    d.start_task('geo', 'configure', 'geo'); d.feed('geo', 'checking compiler'); d.finish_task('geo', ok=True, final=False)
    d.start_task('geo', 'build', 'geo'); d.feed('geo', 'compiling x.cpp'); d.finish_task('geo', ok=True, final=True)
    assert 'checking compiler' in log.text and 'compiling x.cpp' in log.text  # full buffer across phases
    assert 'geo' in log.text and '====' in log.text                          # one delimited per-target block


def test_log_writes_permanent_lines_and_closes():
    log = _Log()
    d, _, _ = _disp(isatty=False, log=log)
    d.print_above('Built 3 target(s)')
    d.close()
    assert 'Built 3 target(s)' in log.text


def test_the_summary_a_closed_display_no_longer_owns_still_reaches_the_log(capsys):
    # the build summary and the diagnostics print after close(), and an error there must be traceable
    log = _Log()
    d, _, _ = _disp(isatty=False, log=log)
    d.start_task('geo', 'build', 'geo'); d.feed('geo', 'compiling x.cpp'); d.finish_task('geo', ok=True)
    d.close()
    system.set_run_log(log)
    try:
        system.console('Built 27 target(s) in 5m 22s')
        system.warning('  geo: 1 warning(s)')
    finally:
        system.set_run_log(None)
    assert log.text.index('compiling x.cpp') < log.text.index('Built 27 target(s)')  # after every target
    assert '1 warning(s)' in log.text
    assert 'Built 27 target(s)' in capsys.readouterr().out  # and still on the terminal


def test_region_line_has_no_trailing_padding_before_a_preview_arrives():
    d, _, clk = _disp(isatty=True)
    d.start_task('t', 'configure', 'rpclib')
    line = d._task_line(d._tasks['t'], clk(), 200)
    assert line == line.rstrip()          # trailing pad appears as stray spaces in the region
    d.feed('t', 'compiling foo.cpp')
    assert d._task_line(d._tasks['t'], clk(), 200).endswith('compiling foo.cpp')


def test_replay_shows_only_the_failing_phase_not_the_whole_dep_history():
    d, out, clk = _disp(isatty=True)
    d.start_task('t', 'check', 'rpcservice')
    d.feed('t', '- Target rpcservice BUILD [not built yet]')   # load-phase status, minutes old
    clk.tick(); d.finish_task('t', ok=True, final=False)
    d.start_task('t', 'build', 'rpcservice')
    d.feed('t', 'error: undefined reference to foo()')
    clk.tick(); d.finish_task('t', ok=False)
    out.truncate(0); out.seek(0)
    d.replay('t')
    text = strip(out.getvalue())
    assert 'undefined reference' in text and 'not built yet' not in text


CMAKE_WARN = [
    'CMake Warning:',
    '  Manually-specified variables were not used by the project:',
    '',
    '    BUILD_APPS',
    '    BUILD_TESTING',
    '',
    '',
    '-- Configuring done (0.2s)',
]


def test_a_cmake_block_keeps_its_body_not_just_the_header():
    diags, n_err, n_warn = scan_diagnostics(CMAKE_WARN)
    assert (n_err, n_warn) == (0, 1)
    text = diags[0][1]
    assert text.startswith('CMake Warning:')
    assert 'Manually-specified variables' in text and 'BUILD_APPS' in text  # the body must survive, not only the header
    assert 'Configuring done' not in text                                   # the blank pair ends the block


def test_a_cmake_block_ends_at_unindented_output():
    diags, _, _ = scan_diagnostics(['CMake Warning at foo.cmake:3 (message):', '  careful', 'ninja: build stopped'])
    assert 'careful' in diags[0][1] and 'ninja' not in diags[0][1]


def test_a_single_line_compiler_diagnostic_is_unchanged():
    diags, n_err, _ = scan_diagnostics(['foo.cpp(12): error C2065: undeclared identifier', '  some other output'])
    assert n_err == 1 and diags[0][1] == 'foo.cpp(12): error C2065: undeclared identifier'


def test_a_runaway_cmake_block_is_capped_and_says_so():
    lines = ['CMake Warning:'] + [f'  line {i}' for i in range(40)]
    diags, _, n_warn = scan_diagnostics(lines)
    text = diags[0][1]
    assert len(text.split('\n')) == 10               # header + _MAX_BODY + the truncation note
    assert '(+32 more lines)' in text                 # capped, but not silently
    assert n_warn == 1                                # body lines are not re-scanned as their own diagnostics


def test_a_render_after_close_cannot_redraw_over_the_final_output():
    # the CPU sampler can outlive close()'s 1s join and would redraw the region below the build summary
    d, out, clk = _disp(isatty=True)
    d.start_task('t', 'build', 'protobuf'); clk.tick(2.0)
    d.close()
    out.truncate(0); out.seek(0)
    d.render(force=True)
    assert out.getvalue() == ''


def test_print_above_after_close_still_reaches_the_terminal():
    # console() can race between display.close() and set_active_display(None). The line must not vanish.
    d, out, clk = _disp(isatty=True)
    d.close()
    out.truncate(0); out.seek(0)
    d.print_above('  - Target foo   OK')
    assert 'Target foo' in strip(out.getvalue())


_GCC_TEMPLATE_ERROR = [
    '[42/180] Building CXX object CMakeFiles/krattgcs.dir/src/main.cpp.o',
    'In file included from /w/src/link.h:9,',
    '                 from /w/src/main.cpp:3:',
    "/w/pkg/rpp/delegate.h: In instantiation of 'void rpp::delegate<R(A ...)>::reset(Obj*, MemFun)':",
    "/w/src/link.h:120:31:   required from 'void Link::bind() [with T = Telemetry]'",
    '/w/src/main.cpp:88:17:   required from here',
    "/w/pkg/rpp/delegate.h:566:27: error: must use '.*' or '->*' to call pointer-to-member function",
    '  566 |         return (obj->func)(args...);',
    '      |                 ~~~~~~~~~^~~~~~~~~~',
    'ninja: build stopped: subcommand failed.',
]


def test_a_template_error_keeps_the_instantiation_site():
    """One line pointing inside a header nobody edited is useless - the bug lives at the call site that
    instantiated the template, which GCC prints ABOVE the error."""
    diags, n_err, _ = scan_diagnostics(_GCC_TEMPLATE_ERROR)
    text = diags[0][1]
    assert n_err == 1
    assert '/w/src/main.cpp:88:17:   required from here' in text   # the call site the user has to fix
    assert 'In instantiation of' in text
    assert 'return (obj->func)(args...);' in text                  # the expression that broke
    assert 'ninja: build stopped' not in text


def test_a_clang_template_error_keeps_the_note_that_names_the_site():
    # clang reports the instantiation site in a `note:` AFTER the error, not above it
    diags, n_err, _ = scan_diagnostics([
        "/w/src/main.cpp:88:17: error: no matching member function for call to 'reset'",
        '   88 |     d.reset(this, &Link::onData);',
        '      |     ~~^~~~~',
        "/w/pkg/rpp/delegate.h:566:10: note: in instantiation of function template specialization "
        "'Link::bind<Telemetry>' requested here",
        'ninja: build stopped: subcommand failed.'])
    assert n_err == 1 and "'Link::bind<Telemetry>' requested here" in diags[0][1]


def test_a_deep_instantiation_chain_keeps_the_innermost_frames():
    lines = ([f"/w/h.h:{i}:1:   required from 'void f{i}()'" for i in range(20)]
             + ['/w/h.h:99:1: error: no match'])
    text = scan_diagnostics(lines)[0][0][1]
    assert text.count('required from') == 5          # _MAX_CONTEXT, not all 20
    assert "'void f19()'" in text and "'void f0()'" not in text  # the frames nearest the error


def test_context_lines_are_not_re_reported_as_their_own_diagnostics():
    _, n_err, n_warn = scan_diagnostics(_GCC_TEMPLATE_ERROR)
    assert (n_err, n_warn) == (1, 0)


def test_an_inlined_system_header_warning_keeps_the_callers_that_inlined_it():
    """A -Wstringop-overread inside string_fortified.h is unfixable as one line: the caller GCC inlined
    it into is the code that passes the wrong size. That chain has no file prefix on its header line."""
    diags, _, n_warn = scan_diagnostics([
        "In function 'void* memcpy(void*, const void*, size_t)',",
        "    inlined from 'void Telemetry::packHeader(const Frame&)' at /w/src/telemetry.cpp:212:11,",
        "    inlined from 'void Telemetry::send(const Frame&)' at /w/src/telemetry.cpp:240:9:",
        '/usr/include/bits/string_fortified.h:29:33: warning: reading 50 bytes from a region of size 35',
        '   29 |   return __builtin___memcpy_chk (__dest, __src, __len);',
        "/w/src/telemetry.cpp:208:18: note: source object 'hdr' of size 35"])
    text = diags[0][1]
    assert n_warn == 1
    assert '/w/src/telemetry.cpp:240:9' in text     # the caller that passes the wrong size
    assert "note: source object 'hdr' of size 35" in text
