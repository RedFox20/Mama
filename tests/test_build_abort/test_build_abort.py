"""Pins the cooperative shutdown: the flag stops new work, every phase gate closes, a live child gets
a grace period to stop on its own, and only a child that ignores the request gets killed."""
import os
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from mama import dependency_chain as dc
from mama.build_scheduler import BUILD, Job, Scheduler
from mama.util import GitError
from mama.utils import abort, sub_process
from mama.utils.abort import BuildAborted
from mama.utils.sub_process import SubProcess
from testutils import make_load_root, strip_ansi

PY = sys.executable
_GRACE = 0.3  # the real 1.0s grace makes every kill test 1s slower for no extra coverage


def test_the_first_reason_wins():
    abort.request('geo failed')
    abort.request('rpcservice failed')  # a consequence of the first: it must not overwrite the cause
    assert abort.requested() and abort.reason() == 'geo failed'


def test_check_raises_the_reason_and_clear_re_arms():
    abort.request('geo failed')
    with pytest.raises(BuildAborted, match='geo failed'):
        abort.check()
    abort.clear()
    assert not abort.requested()
    abort.check()  # no raise: a later build in the same process starts clean


def test_a_flagged_build_spawns_nothing():
    abort.request('geo failed')
    with patch('mama.utils.sub_process.subprocess.Popen') as popen, pytest.raises(BuildAborted):
        SubProcess.run([PY, '-c', 'pass'])
    popen.assert_not_called()  # the flag's purpose: no new child, not a child killed after spawn


def _phase_display():
    return SimpleNamespace(start_task=Mock(), feed=lambda t, l: None, relabel=Mock(), finish_task=Mock())


@pytest.mark.parametrize('kind', ['load', 'configure', 'build'])
def test_no_phase_transitions_while_stopping(kind):
    dep = SimpleNamespace(name='geo', config=SimpleNamespace(verbose=False), phase_times={},
                          is_real_clone=lambda: True, get_children=lambda: [], is_root=False)
    display, body = _phase_display(), Mock()
    abort.request('rpcservice failed')
    with pytest.raises(BuildAborted):
        dc._run_phase(display, dep, kind, body, build_slot=None)
    body.assert_not_called()
    display.start_task.assert_not_called()  # a phase that never ran must not appear as a failed one


def test_a_queued_load_does_not_clone_while_stopping():
    root = make_load_root()
    abort.request('geo failed')
    with pytest.raises(BuildAborted):
        dc.load_dependency_chain(root)
    root.load.assert_not_called()


def test_a_failed_load_stops_the_pool_before_exiting():
    root = make_load_root()
    root.load.side_effect = GitError('[CLONE FAILED]  mylib')
    with patch('mama.dependency_chain.SubProcess.terminate_all') as stop, pytest.raises(SystemExit):
        dc.load_dependency_chain(root)
    stop.assert_called_once_with('load failed')  # else the pool clones its whole backlog first


def test_a_stopped_job_reports_one_line_without_a_traceback(capsys):
    dc._report_error(BuildAborted('build stopped: geo failed'), verbose=False)
    out = strip_ansi(capsys.readouterr().out)
    assert 'build stopped: geo failed' in out and 'Traceback' not in out


def test_the_scheduler_names_the_failed_job_as_the_reason():
    reasons = []
    sched = Scheduler(max_configure=2, core_budget=2, abort_hook=reasons.append)
    def boom(): raise RuntimeError('kaboom')
    sched.run([Job('geo', BUILD, boom, node=SimpleNamespace(name='geo'))])
    assert reasons == ['geo failed']


# -- the grace period, against real child processes --------------------------------------------------

def _run_child(*args):
    """Start `args` as a real captured child, wait until it reports itself ready, and return
    (output lines, the live SubProcess). Waiting for the process to register is not enough: a signal
    that lands during interpreter startup kills the child before it installs its own handler."""
    lines = []
    def run():
        # A killed child can raise out of run() (reader hits a closed PTY). Unhandled in a thread, pytest logs a teardown error.
        try: SubProcess.run([PY, *args], io_func=lambda p, l: lines.append(l))
        except BaseException: pass
    threading.Thread(target=run, daemon=True).start()
    end = time.monotonic() + 10
    while time.monotonic() < end and 'ready' not in lines: time.sleep(0.01)
    assert 'ready' in lines, lines
    procs = list(sub_process._live_procs)
    assert len(procs) == 1, procs
    return lines, procs[0]


_COOPERATIVE = ('import signal, sys, time\n'
                'signal.signal(signal.SIGINT, lambda *a: (print("cleaned up", flush=True), sys.exit(0)))\n'
                'print("ready", flush=True)\n'
                'time.sleep(30)\n')

_STUBBORN = ('import signal, time\n'
             'signal.signal(signal.SIGINT, signal.SIG_IGN)\n'
             'print("ready", flush=True)\n'
             'time.sleep(30)\n')

# argv: the kid's SIGINT-ignored marker file, then the kid pid file. The parent stops, so only the group sweep stops the kid.
_PARENT_OF_A_STUBBORN_KID = '''
import os, subprocess, sys, time
ready, pidfile = sys.argv[1], sys.argv[2]
kid = subprocess.Popen([sys.executable, '-c', "import signal, sys, time;"
                        " signal.signal(signal.SIGINT, signal.SIG_IGN);"
                        " open(sys.argv[1], 'w').close(); time.sleep(30)", ready])
open(pidfile, 'w').write(str(kid.pid))
while not os.path.exists(ready): time.sleep(0.01)
print('ready', flush=True)
time.sleep(30)
'''


@pytest.mark.skipif(os.name == 'nt', reason='SIGINT to a process group is UNIX only')
def test_a_cooperative_child_stops_itself_and_is_never_killed():
    lines, child = _run_child('-c', _COOPERATIVE)
    SubProcess.terminate_all('geo failed', grace=_GRACE)
    assert not child._killed      # it stopped on the request, so mama never had to kill it
    assert 'cleaned up' in lines  # and it removed its own leftovers, which a hard kill would skip


@pytest.mark.skipif(os.name == 'nt', reason='SIGINT to a process group is UNIX only')
def test_a_stubborn_child_is_killed_after_the_grace():
    _, child = _run_child('-c', _STUBBORN)
    start = time.monotonic()
    SubProcess.terminate_all('geo failed', grace=_GRACE)
    assert child._killed                       # it ignored the request, so the grace ran out
    assert time.monotonic() - start >= _GRACE   # and it got the full grace first


@pytest.mark.skipif(os.name == 'nt', reason='SIGINT to a process group is UNIX only')
def test_a_grandchild_that_missed_the_request_still_dies(tmp_path):
    """cmake stops politely, but its ninja can miss the group signal (mid-exec when it lands). Nothing
    kills the direct child then, so the group sweep is the only thing left to stop the grandchild."""
    psutil = pytest.importorskip('psutil')
    script, ready, pidfile = (str(tmp_path / n) for n in ('parent.py', 'ready', 'kid.pid'))
    open(script, 'w').write(_PARENT_OF_A_STUBBORN_KID)
    _run_child(script, ready, pidfile)  # 'ready' means the kid ignores SIGINT from here on
    kid = int(open(pidfile).read())
    SubProcess.terminate_all('geo failed', grace=_GRACE)
    end = time.monotonic() + 5
    while time.monotonic() < end and psutil.pid_exists(kid): time.sleep(0.02)
    alive = psutil.pid_exists(kid)
    if alive:
        try: psutil.Process(kid).kill()  # do not leak a 30s sleeper when the assert fails
        except Exception: pass
    assert not alive


def test_a_failed_load_stops_the_clones_already_running():
    """The property the two-stage stop exists for: one failed load does not make the user wait for
    every clone the thread pool already started."""
    def clone(): SubProcess.run([PY, '-c', 'import time; time.sleep(10)'], io_func=lambda p, l: None)
    def fail():
        end = time.monotonic() + 10  # fail only once the sibling clones really run, else this proves nothing
        while time.monotonic() < end and len(sub_process._live_procs) < 3: time.sleep(0.01)
        assert len(sub_process._live_procs) == 3
        raise GitError('[CLONE FAILED]  geo')
    kids = [Mock(already_loaded=False, should_rebuild=False, **{'get_children.return_value': [],
                 'load.side_effect': clone}) for _ in range(4)]
    kids[0].load.side_effect = fail
    root = make_load_root(serial_load=False, parallel_load=True)
    root.get_children.return_value = kids
    start = time.monotonic()
    with pytest.raises(SystemExit):
        dc.load_dependency_chain(root)
    # The clones stop on the request, so the grace never runs out. Without the stop, the pool shutdown drains the full 10s clones.
    assert time.monotonic() - start < 3.0


def test_terminate_all_returns_at_once_when_no_child_runs():
    start = time.monotonic()
    SubProcess.terminate_all('geo failed', grace=5.0)
    assert time.monotonic() - start < 1.0  # nothing to wait for: never pay the grace
