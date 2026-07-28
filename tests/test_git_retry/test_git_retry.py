"""Clone retry on a dropped connection, and the reactive connection pacer it turns on."""
import threading
from unittest.mock import Mock, patch

import pytest

from mama.types.git import Git, is_transient_git_failure
from mama.utils import ssh_multiplex as sm


def _git_and_dep():
    git = Git(name='libfoo', url='git@example.com:foo/libfoo.git', branch='main', tag='',
              mamafile=None, shallow=True, args=[])
    dep = Mock(name_='libfoo')
    dep.name = 'libfoo'
    dep.config = Mock(print=False, verbose=False, update_stats=Mock())
    return git, dep


@pytest.fixture(autouse=True)
def unpaced(monkeypatch):
    """The pacer is process-global state and sleeps for real; keep it off unless a test asks for it."""
    monkeypatch.setattr(sm, '_connect_interval', 0.0)
    monkeypatch.setattr(sm, '_last_connect', 0.0)
    monkeypatch.setattr('mama.types.git.time.sleep', lambda s: None)


@pytest.mark.parametrize('output', [
    'kex_exchange_identification: Connection closed by remote host',
    'fatal: unable to access https://git.ffmpeg.org/ffmpeg.git/: Failed to connect to git.ffmpeg.org',
    'mux_client_request_session: session request failed: Session open refused by peer',
    'error: RPC failed; curl 92 HTTP/2 stream 5 was not closed cleanly',
    'You have exceeded a secondary rate limit. Please wait a few minutes',
    '[mama] git stalled 30s, killed (auth prompt or hung server)',  # mama's own idle-timeout kill
])
def test_a_dropped_connection_is_transient(output):
    assert is_transient_git_failure(output)


@pytest.mark.parametrize('output', [
    "fatal: couldn't find remote ref v9.9.9",
    'ERROR: Repository not found.',
    'Permission denied (publickey).',
])
def test_a_bad_url_ref_or_key_is_not_transient(output):
    assert not is_transient_git_failure(output)


def _run_clone(git, dep, results, monkeypatch):
    """Drive clone_with_filtered_progress over a scripted list of (exit_code, output) attempts."""
    attempts = iter(results)
    monkeypatch.setattr(Git, '_run_git_with_filtered_progress',
                        lambda *a, **k: (*next(attempts), '1.0s'))
    monkeypatch.setattr('mama.types.git.remove_tree', lambda d: None)
    git.clone_with_filtered_progress(dep, '--depth 1 url', '/tmp/target')


def test_a_transient_clone_failure_is_retried(monkeypatch):
    git, dep = _git_and_dep()
    _run_clone(git, dep, [(128, 'kex_exchange_identification: Connection closed'), (0, '')], monkeypatch)
    dep.config.update_stats.record_clone.assert_called_once()


def test_a_permanent_clone_failure_raises_on_the_first_attempt(monkeypatch):
    git, dep = _git_and_dep()
    calls = []
    monkeypatch.setattr(Git, '_run_git_with_filtered_progress',
                        lambda *a, **k: (calls.append(1), (128, 'Permission denied (publickey).', '1.0s'))[1])
    with pytest.raises(RuntimeError):
        git.clone_with_filtered_progress(dep, '--depth 1 url', '/tmp/target')
    assert len(calls) == 1  # retrying a denied key only makes the build slower before it fails


def test_retries_are_bounded_and_then_raise(monkeypatch):
    from mama.types.git import _CLONE_ATTEMPTS
    git, dep = _git_and_dep()
    calls = []
    monkeypatch.setattr(Git, '_run_git_with_filtered_progress',
                        lambda *a, **k: (calls.append(1), (128, 'Connection reset by peer', '1.0s'))[1])
    monkeypatch.setattr('mama.types.git.remove_tree', lambda d: None)
    with pytest.raises(RuntimeError):
        git.clone_with_filtered_progress(dep, '--depth 1 url', '/tmp/target')
    assert len(calls) == _CLONE_ATTEMPTS


def test_the_partial_tree_is_removed_before_a_retry(monkeypatch):
    git, dep = _git_and_dep()
    removed = []
    monkeypatch.setattr('mama.types.git.remove_tree', removed.append)
    attempts = iter([(128, 'Connection reset by peer'), (0, '')])
    monkeypatch.setattr(Git, '_run_git_with_filtered_progress', lambda *a, **k: (*next(attempts), '1.0s'))
    git.clone_with_filtered_progress(dep, '--depth 1 url', '/tmp/target')
    assert removed == ['/tmp/target']  # a second clone into a non-empty dir fails on the dir, hiding the real error


def test_a_retry_turns_the_connection_pacer_on(monkeypatch):
    git, dep = _git_and_dep()
    _run_clone(git, dep, [(128, 'Connection reset by peer'), (0, '')], monkeypatch)
    assert sm._connect_interval == sm.THROTTLED_CONNECT_INTERVAL


def test_the_pacer_costs_nothing_until_a_host_pushes_back():
    slept = []
    with patch('mama.utils.ssh_multiplex.time.sleep', slept.append):
        for _ in range(20): sm.pace_new_connection()
    assert slept == []


def test_the_pacer_staggers_once_it_is_on(monkeypatch):
    monkeypatch.setattr(sm, '_connect_interval', sm.THROTTLED_CONNECT_INTERVAL)
    slept = []
    monkeypatch.setattr('mama.utils.ssh_multiplex.time.sleep', slept.append)
    threads = [threading.Thread(target=sm.pace_new_connection) for _ in range(4)]
    for t in threads: t.start()
    for t in threads: t.join()
    # the sleep is faked, so time never advances: every call after the first waits a full interval
    assert len(slept) >= 3 and max(slept) <= sm.THROTTLED_CONNECT_INTERVAL
