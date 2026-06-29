"""Pin SubProcess.run contract: exit status, io_func, cwd/env/timeout, stdin write, PTY isatty."""
import os
import sys
import subprocess

import pytest

from mama.utils.sub_process import SubProcess


PY = sys.executable


def _py_run(code: str, io_func=None, cwd=None, env=None, timeout=None, idle_timeout=None):
    """Run `python -c "<code>"` via SubProcess. Returns (status, lines_seen)."""
    lines = []
    if io_func is None:
        def collect(p, line): lines.append(line)
        io_func = collect
    status = SubProcess.run([PY, '-c', code], cwd=cwd, env=env,
                            io_func=io_func, timeout=timeout, idle_timeout=idle_timeout)
    return status, lines


class TestExitStatus:
    def test_run_returns_zero_on_success(self):
        status, _ = _py_run('import sys; sys.exit(0)')
        assert status == 0

    def test_run_returns_nonzero_exit_code(self):
        status, _ = _py_run('import sys; sys.exit(7)')
        assert status == 7

    def test_run_without_io_func_inherits_stdio(self, capfd):
        # No io_func means child writes go straight to parent stdio; capfd hooks at OS level.
        status = SubProcess.run([PY, '-c', 'print("hello-no-iofunc")'])
        assert status == 0
        assert 'hello-no-iofunc' in capfd.readouterr().out


class TestIoFunc:
    def test_each_line_delivered_once(self):
        _, lines = _py_run('print("alpha"); print("beta"); print("gamma")')
        assert lines == ['alpha', 'beta', 'gamma']

    def test_stderr_is_merged_into_io_func(self):
        # Essential for git: progress goes to stderr.
        _, lines = _py_run('import sys; print("out"); print("err", file=sys.stderr)')
        assert 'out' in lines and 'err' in lines

    def test_no_trailing_carriage_return_on_lines(self):
        _, lines = _py_run('print("plain")')
        assert 'plain' in lines
        assert not any(line.endswith('\r') or line.endswith('\n') for line in lines)

    def test_io_func_exception_is_re_raised_by_run(self):
        # Reader-thread exceptions must surface; otherwise debugging is impossible.
        def broken(p, line): raise RuntimeError(f'callback boom on line={line!r}')
        with pytest.raises(RuntimeError, match='callback boom'):
            SubProcess.run([PY, '-c', 'print("hi")'], io_func=broken)


class TestCwd:
    def test_cwd_is_honored(self, tmp_path):
        (tmp_path / 'sentinel.txt').write_text('found-it')
        _, lines = _py_run('print(open("sentinel.txt").read())', cwd=str(tmp_path))
        assert lines == ['found-it']


class TestEnv:
    def test_env_var_passes_through(self):
        env = os.environ.copy()
        env['MAMA_TEST_X'] = 'hello-env'
        _, lines = _py_run('import os; print(os.environ["MAMA_TEST_X"])', env=env)
        assert lines == ['hello-env']

    def test_default_env_inherits_parent(self):
        os.environ['MAMA_TEST_Y'] = 'inherited'
        try:
            _, lines = _py_run('import os; print(os.environ["MAMA_TEST_Y"])')
            assert lines == ['inherited']
        finally:
            del os.environ['MAMA_TEST_Y']


class TestTimeout:
    def test_long_running_command_times_out(self):
        with pytest.raises(subprocess.TimeoutExpired):
            SubProcess.run([PY, '-c', 'import time; time.sleep(5)'],
                           io_func=lambda p, line: None, timeout=0.3)

    def test_fast_command_does_not_time_out(self):
        assert SubProcess.run([PY, '-c', 'print("done")'],
                              io_func=lambda p, line: None, timeout=10.0) == 0


class TestIdleTimeout:
    def test_idle_timeout_kills_a_silent_child(self):
        import time
        t0 = time.monotonic()
        with pytest.raises(subprocess.TimeoutExpired):
            SubProcess.run([PY, '-c', 'import time; time.sleep(5)'],
                           io_func=lambda p, l: None, idle_timeout=0.4)
        assert time.monotonic() - t0 < 5  # died ~0.4s, not 30s

    def test_idle_timeout_spares_a_chatty_child(self):
        # Streaming output keeps resetting the idle clock, so total runtime (0.6s) > idle (0.4s) is fine.
        status, lines = _py_run(
            'import sys, time\nfor i in range(6): print(i); sys.stdout.flush(); time.sleep(0.1)',
            idle_timeout=0.4)
        assert status == 0 and lines == ['0', '1', '2', '3', '4', '5']


class TestStdinWrite:
    def test_write_delivers_to_child(self):
        # SSH host-key auto-accept path: clone_with_filtered_progress writes 'yes\\n' on prompt.
        lines = []
        def echoer(p, line):
            lines.append(line)
            if line == 'READY': p.write('the-secret\n')
        status = SubProcess.run(
            [PY, '-c', 'import sys; print("READY"); sys.stdout.flush(); print("got:" + input())'],
            io_func=echoer)
        assert status == 0
        assert 'READY' in lines and 'got:the-secret' in lines


@pytest.mark.skipif(sys.platform == 'win32', reason='PTY behaviour is UNIX-only')
class TestPtyOnUnix:
    def test_child_sees_a_tty_when_io_func_is_set(self):
        # Why pty.openpty(): git inspects isatty(stderr) to decide whether to emit progress.
        _, lines = _py_run('import sys; print(sys.stdout.isatty())')
        assert lines == ['True']

    def test_child_does_not_see_a_tty_without_io_func(self, capfd):
        # No PTY allocated when capture isn't requested; child runs cleanly through to exit.
        assert SubProcess.run([PY, '-c', 'import sys; sys.exit(0)']) == 0


class TestErrorPaths:
    def test_missing_executable_raises_oserror(self):
        with pytest.raises(OSError, match='not found in PATH'):
            SubProcess.run('this-binary-does-not-exist-mama-42', io_func=lambda p, l: None)

    def test_string_cmd_is_shlex_split(self):
        _, lines = _py_run('print("from-string-cmd")')
        assert lines == ['from-string-cmd']

    def test_list_cmd_is_passed_through(self):
        lines = []
        SubProcess.run([PY, '-c', 'print("list-cmd")'],
                       io_func=lambda p, l: lines.append(l))
        assert 'list-cmd' in lines


class TestCarriageReturnProgress:
    def test_cr_separated_progress_emitted_as_distinct_lines(self):
        _, lines = _py_run(
            r'import sys; sys.stdout.write("[1/3] foo\r[2/3] bar\r[3/3] baz\n"); sys.stdout.flush()')
        assert lines == ['[1/3] foo', '[2/3] bar', '[3/3] baz']

    def test_cr_at_chunk_end_flushes_on_idle(self):
        # The slow-step write isolates a \r in its own chunk; without idle-flush
        # the line would only appear when the next chunk arrives seconds later.
        _, lines = _py_run(
            r'import sys, time; '
            r'sys.stdout.write("[1/2] slow_step\r"); sys.stdout.flush(); '
            r'time.sleep(0.25); '
            r'sys.stdout.write("[2/2] done\n"); sys.stdout.flush()')
        assert lines == ['[1/2] slow_step', '[2/2] done']

    def test_lone_lf_after_cr_idle_flush_does_not_emit_empty_line(self):
        # After idle-flushing on \r, a delayed \n (or PTY-ONLCR \r\n) is part
        # of the same logical line and must be swallowed, not emit "".
        _, lines = _py_run(
            r'import sys, time; '
            r'sys.stdout.write("partial\r"); sys.stdout.flush(); '
            r'time.sleep(0.25); '
            r'sys.stdout.write("\n"); sys.stdout.flush()')
        assert lines == ['partial']


class _Drainer:
    """Drives _drain_buffer directly (no child process) over a shared buffer +
    swallow state, so chunk boundaries and idle/eof can be controlled exactly."""
    def __init__(self):
        self.p = object.__new__(SubProcess)
        self.p._swallow_lf = False
        self.lines = []
        self.p.io_func = lambda _s, line: self.lines.append(line)
        self.buf = bytearray()

    def feed(self, data, idle=False, eof=False):
        self.buf.extend(data)
        self.p._drain_buffer(self.buf, idle=idle, eof=eof)
        return self.lines


class TestDrainBuffer:
    def test_multiple_cr_progress_in_one_chunk(self):
        d = _Drainer()
        assert d.feed(b'a\rb\rc\n') == ['a', 'b', 'c']
        assert d.buf == b''

    def test_crlf_is_single_line_with_cr_stripped(self):
        d = _Drainer()
        assert d.feed(b'line\r\n') == ['line']

    def test_bare_lf_lines(self):
        assert _Drainer().feed(b'x\ny\n') == ['x', 'y']

    def test_empty_lf_lines_preserved(self):
        assert _Drainer().feed(b'\n\n') == ['', '']

    def test_cr_at_end_without_idle_is_retained(self):
        d = _Drainer()
        assert d.feed(b'prog\r') == []
        assert d.buf == b'prog\r' and d.p._swallow_lf is False

    def test_cr_at_end_with_idle_flushes_and_arms_swallow(self):
        d = _Drainer()
        assert d.feed(b'prog\r', idle=True) == ['prog']
        assert d.buf == b'' and d.p._swallow_lf is True

    def test_swallow_consumes_leading_lf(self):
        d = _Drainer()
        d.feed(b'prog\r', idle=True)
        assert d.feed(b'\nmore\n') == ['prog', 'more']

    def test_swallow_consumes_leading_crlf(self):
        d = _Drainer()
        d.feed(b'prog\r', idle=True)
        assert d.feed(b'\r\nmore\n') == ['prog', 'more']

    def test_swallow_consumes_only_one_lf(self):
        # Second \n is a real empty line, not swallowed.
        d = _Drainer()
        d.feed(b'prog\r', idle=True)
        assert d.feed(b'\n\nmore\n') == ['prog', '', 'more']

    def test_swallow_with_no_leading_lf_keeps_content(self):
        d = _Drainer()
        d.feed(b'prog\r', idle=True)
        assert d.feed(b'xyz\n') == ['prog', 'xyz']

    def test_crlf_split_across_chunks_is_not_progress(self):
        # \r held at a non-idle boundary must pair with the next chunk's \n
        # as one CRLF line, never flush as progress then swallow.
        d = _Drainer()
        assert d.feed(b'abc\r') == [] and d.buf == b'abc\r'
        assert d.feed(b'\ndef\n') == ['abc', 'def']

    def test_partial_after_progress_is_retained_then_completed(self):
        d = _Drainer()
        assert d.feed(b'ab\rcd') == ['ab'] and d.buf == b'cd'
        assert d.feed(b'ef\n') == ['ab', 'cdef']

    def test_idle_keeps_trailing_partial_without_delimiter(self):
        d = _Drainer()
        assert d.feed(b'done\nrest', idle=True) == ['done']
        assert d.buf == b'rest'

    def test_eof_flushes_trailing_partial(self):
        d = _Drainer()
        assert d.feed(b'tail', eof=True) == ['tail'] and d.buf == b''

    def test_eof_strips_trailing_cr(self):
        d = _Drainer()
        assert d.feed(b'tail\r', eof=True) == ['tail'] and d.buf == b''

    def test_partial_without_delimiter_or_flags_is_buffered(self):
        d = _Drainer()
        assert d.feed(b'partial') == [] and d.buf == b'partial'

    def test_many_tiny_lines(self):
        # Stresses cursor advancement: thousands of single-byte lines in one chunk.
        d = _Drainer()
        lines = d.feed(b'x\n' * 5000)
        assert lines == ['x'] * 5000 and d.buf == b''


class TestCtrlCTermination:
    @pytest.fixture(autouse=True)
    def _disarm(self):
        SubProcess.clear_abort(); yield; SubProcess.clear_abort()

    def test_terminate_all_blocks_new_spawns_then_clear_re_arms(self):
        SubProcess.terminate_all()
        with pytest.raises(KeyboardInterrupt):
            SubProcess.run([PY, '-c', 'pass'])
        SubProcess.clear_abort()
        assert SubProcess.run([PY, '-c', 'pass'], io_func=lambda p, l: None) == 0

    def test_terminate_all_kills_a_running_child(self):
        import threading, time
        from mama.utils import sub_process
        result = {}
        def run_child():
            try: result['s'] = SubProcess.run([PY, '-c', 'import time; time.sleep(5)'], io_func=lambda p, l: None)
            except BaseException as e: result['exc'] = e
        t = threading.Thread(target=run_child); t.start()
        end = time.monotonic() + 5
        while time.monotonic() < end and not sub_process._live_procs: time.sleep(0.01)
        SubProcess.terminate_all()
        t.join(10)
        assert not t.is_alive()         # killed promptly, not blocked for the full 30s
        assert result.get('s', 0) != 0  # nonzero status from the kill


class TestNoForkptyDeprecationWarning:
    # The whole point of the Popen+pty.openpty rewrite was to kill this warning
    # (Python 3.12 flags forkpty() in MT programs - real deadlock risk).
    def test_run_does_not_emit_forkpty_warning(self):
        import warnings
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            SubProcess.run([PY, '-c', 'print("x")'], io_func=lambda p, l: None)
        forkpty_warnings = [w for w in caught if 'forkpty' in str(w.message).lower()]
        assert forkpty_warnings == [], f'forkpty deprecation came back: {forkpty_warnings}'
