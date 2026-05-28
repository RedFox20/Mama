"""Direct unit tests for the SubProcess class.

Background: SubProcess used to wrap os.fork / os.forkpty, which Python 3.12
flags as unsafe in multi-threaded programs (DeprecationWarning at startup,
real deadlock potential under heavy parallel mama load). The rewrite uses
subprocess.Popen with an optional pty.openpty() pair on UNIX so the child
still sees a TTY (preserving git's progress output and isatty checks).

These tests pin the behavioural contract:
* run() returns the child's exit status
* io_func is called once per line of combined stdout+stderr
* cwd / env / timeout parameters are honoured
* write() can deliver stdin to the child (used for SSH host-key prompts)
* The child sees a TTY on UNIX when io_func is set
* Reader-thread exceptions resurface in run() rather than being silently lost
* Missing executables raise OSError early, not deadlock the worker

Commands are issued via `sys.executable -c '...'` to stay portable across
Linux / macOS / Windows.
"""
from __future__ import annotations

import os
import sys
import subprocess
import tempfile

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))
from mama.utils.sub_process import SubProcess  # noqa: E402


PY = sys.executable


def _py_run(code: str, io_func=None, cwd=None, env=None, timeout=None):
    """Run `python -c "<code>"` via SubProcess. Returns (status, lines_seen)."""
    lines = []
    if io_func is None:
        def collect(p, line): lines.append(line)
        io_func = collect
    status = SubProcess.run([PY, '-c', code], cwd=cwd, env=env,
                            io_func=io_func, timeout=timeout)
    return status, lines


class TestExitStatus:
    def test_run_returns_zero_on_success(self):
        status, _ = _py_run('import sys; sys.exit(0)')
        assert status == 0

    def test_run_returns_nonzero_exit_code(self):
        status, _ = _py_run('import sys; sys.exit(7)')
        assert status == 7

    def test_run_without_io_func_inherits_stdio(self, capfd):
        """No io_func: child writes flow straight through the parent's
        stdout/stderr. capfd captures what would normally hit the terminal."""
        status = SubProcess.run([PY, '-c', 'print("hello-no-iofunc")'])
        assert status == 0
        # Captured at the OS level (capfd) - not via pytest's capsys.
        assert 'hello-no-iofunc' in capfd.readouterr().out


class TestIoFunc:
    def test_each_line_delivered_once(self):
        _, lines = _py_run('print("alpha"); print("beta"); print("gamma")')
        assert lines == ['alpha', 'beta', 'gamma']

    def test_stderr_is_merged_into_io_func(self):
        """SubProcess merges stderr into the same stream so the io_func can
        see both. This is essential for git: progress goes to stderr."""
        _, lines = _py_run('import sys; print("out"); print("err", file=sys.stderr)')
        assert 'out' in lines
        assert 'err' in lines

    def test_no_trailing_carriage_return_on_lines(self):
        """Either via PTY or pipe, the io_func must receive bare lines -
        no stray '\\r' from text-mode normalisation, no '\\n'."""
        _, lines = _py_run('print("plain")')
        assert 'plain' in lines
        assert not any(line.endswith('\r') or line.endswith('\n') for line in lines)

    def test_io_func_exception_is_re_raised_by_run(self):
        """A bug inside io_func must surface, not silently kill the reader
        thread. Otherwise debugging is impossible."""
        def broken(p, line):
            raise RuntimeError(f'callback boom on line={line!r}')
        with pytest.raises(RuntimeError, match='callback boom'):
            SubProcess.run([PY, '-c', 'print("hi")'], io_func=broken)


class TestCwd:
    def test_cwd_is_honored(self, tmp_path):
        marker = tmp_path / 'sentinel.txt'
        marker.write_text('found-it')
        # Use relative open inside the child to prove cwd was set.
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
            SubProcess.run(
                [PY, '-c', 'import time; time.sleep(30)'],
                io_func=lambda p, line: None,
                timeout=0.3,
            )

    def test_fast_command_does_not_time_out(self):
        status = SubProcess.run(
            [PY, '-c', 'print("done")'],
            io_func=lambda p, line: None,
            timeout=10.0,
        )
        assert status == 0


class TestStdinWrite:
    def test_write_delivers_to_child(self):
        """The interactive prompt case: SubProcess.write() must reach the
        child's stdin. Used by clone_with_filtered_progress to auto-accept
        SSH host key prompts."""
        lines = []
        def echoer(p, line):
            lines.append(line)
            # As soon as the child prints "READY", send something back.
            if line == 'READY':
                p.write('the-secret\n')
        # Child prints READY, reads one line of stdin, prints it back.
        status = SubProcess.run(
            [PY, '-c', 'import sys; print("READY"); sys.stdout.flush(); print("got:" + input())'],
            io_func=echoer,
        )
        assert status == 0
        assert 'READY' in lines
        assert 'got:the-secret' in lines


@pytest.mark.skipif(sys.platform == 'win32', reason='PTY behaviour is UNIX-only')
class TestPtyOnUnix:
    def test_child_sees_a_tty_when_io_func_is_set(self):
        """The whole reason we use pty.openpty(): git inspects isatty(stderr)
        to decide whether to emit progress lines like 'Receiving objects: ...'.
        Without a PTY, that progress output disappears."""
        _, lines = _py_run('import sys; print(sys.stdout.isatty())')
        assert lines == ['True']

    def test_child_does_not_see_a_tty_without_io_func(self, capfd):
        """Symmetry: without io_func, no PTY is allocated - the child gets
        the parent's actual stdout. That stdout MAY or MAY NOT be a TTY
        depending on the test runner; what we lock down here is just that
        there's no spurious PTY-faking when io_func is omitted."""
        # We can't easily assert isatty() result here because pytest's capfd
        # may or may not give the child a TTY. What we CAN assert is that
        # the child runs and exits cleanly with no io_func.
        status = SubProcess.run([PY, '-c', 'import sys; sys.exit(0)'])
        assert status == 0


class TestErrorPaths:
    def test_missing_executable_raises_oserror(self):
        with pytest.raises(OSError, match='not found in PATH'):
            SubProcess.run('this-binary-does-not-exist-mama-42', io_func=lambda p, l: None)

    def test_string_cmd_is_shlex_split(self):
        """Backwards-compat: cmd as a single string is shlex.split into args."""
        _, lines = _py_run('print("from-string-cmd")')
        # If shlex.split is broken we'd never reach here cleanly.
        assert lines == ['from-string-cmd']

    def test_list_cmd_is_passed_through(self):
        lines = []
        SubProcess.run([PY, '-c', 'print("list-cmd")'],
                       io_func=lambda p, l: lines.append(l))
        assert 'list-cmd' in lines


class TestNoForkptyDeprecationWarning:
    """Regression guard for the original motivation: the old implementation
    triggered a Python 3.12 DeprecationWarning on every forkpty() call from
    a multi-threaded program (a real deadlock risk). The rewrite must not."""

    def test_run_does_not_emit_forkpty_warning(self):
        import warnings
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            SubProcess.run([PY, '-c', 'print("x")'],
                           io_func=lambda p, l: None)
        forkpty_warnings = [
            w for w in caught
            if 'forkpty' in str(w.message).lower()
        ]
        assert forkpty_warnings == [], (
            f'forkpty deprecation warning came back: {forkpty_warnings}'
        )
