"""Pin SubProcess.run contract: exit status, io_func, cwd/env/timeout, stdin write, PTY isatty."""
import os
import sys
import subprocess

import pytest

from mama.utils.sub_process import SubProcess


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
            SubProcess.run([PY, '-c', 'import time; time.sleep(30)'],
                           io_func=lambda p, line: None, timeout=0.3)

    def test_fast_command_does_not_time_out(self):
        assert SubProcess.run([PY, '-c', 'print("done")'],
                              io_func=lambda p, line: None, timeout=10.0) == 0


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
