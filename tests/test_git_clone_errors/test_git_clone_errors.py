"""Pins the git failure report: which cause each git output maps to, and the clean GitError that a
failed clone or fetch raises instead of a traceback."""
from unittest.mock import patch

import pytest

from mama import dependency_chain as dc
from mama.types.git import Git, _CLONE_ATTEMPTS
from mama.types.git_errors import classify_git_failure
from mama.utils.errors import GitError
from testutils import make_git_and_mock_dep, make_load_root, strip_ansi

RESET = ('remote: Enumerating objects: 1284923, done.\n'
         'error: RPC failed; curl 35 Recv failure: Connection was reset\n'
         'fatal: expected flush after ref listing')
THROTTLED = ('remote: You have exceeded a secondary rate limit. Please wait a few minutes.\n'
             "fatal: unable to access 'https://github.com/x/y.git/': The requested URL returned error: 403")

# case -> (git output, transient, a phrase the named cause must contain)
_FAILURES = {
    'missing repo': ('remote: Repository not found.\n'
                     "fatal: repository 'https://github.com/kratt/nope.git/' not found", False, 'does not exist'),
    # a 404 over https also prints 'unable to access', which alone reads as a network failure
    'https 404': ("fatal: unable to access 'https://git.example.com/x.git/': "
                  'The requested URL returned error: 404', False, 'does not exist'),
    'bad local url': ("fatal: repository '/no/such/path' does not exist", False, 'does not exist'),
    'no ssh key': ('git@github.com: Permission denied (publickey).\n'
                   'fatal: Could not read from remote repository.', False, 'denied access'),
    'bad credentials': ("fatal: Authentication failed for 'https://gitlab.example.com/x.git/'",
                        False, 'authentication failed'),
    'untrusted host': ('Host key verification failed.\n'
                       'fatal: Could not read from remote repository.', False, 'host key'),
    'missing tag': ('warning: Could not find remote branch n8.0.1 to clone.\n'
                    'fatal: Remote branch n8.0.1 not found in upstream origin', False, 'does not exist on the remote'),
    'full disk': ('fatal: write error: No space left on device', False, 'disk is full'),
    # the rate limit answers 403, which alone reads as a permanent access denial
    'throttled': (THROTTLED, True, 'throttled'),
    'server error': ("fatal: unable to access 'https://x/y.git/': The requested URL returned error: 503",
                     True, 'temporary error'),
    'dropped connection': (RESET, True, 'closed the connection'),
    'refused ssh session': ('mux_client_request_session: session request failed: Session open refused by peer',
                            True, 'closed the connection'),
    'unresolvable host': ("fatal: unable to access 'https://nope/': Could not resolve host: nope",
                          True, 'does not resolve'),
    'unreachable server': ('fatal: unable to access https://git.ffmpeg.org/ffmpeg.git/: '
                           'Failed to connect to git.ffmpeg.org', True, 'cannot reach'),
    'stalled git': ('[mama] git stalled 300s, killed (auth prompt or hung server)', True, 'idle timeout'),
    'unknown': ('fatal: the remote end said something new', False, 'exited with code 128'),
}


@pytest.mark.parametrize('output, transient, reason', _FAILURES.values(), ids=list(_FAILURES))
def test_the_cause_is_named_from_the_git_output(output, transient, reason):
    cause = classify_git_failure(output, 128)
    assert cause.transient is transient
    assert reason in cause.reason and cause.hint


def _failed_clone(output):
    """Drive a clone that fails on every attempt, and return the message of the GitError it raises."""
    git, dep = make_git_and_mock_dep(name='ffmpeg', url='https://git.ffmpeg.org/ffmpeg.git', branch='', tag='n8.0.1')
    attempts = iter([(128, output)] * _CLONE_ATTEMPTS)
    with patch.object(Git, '_run_git_with_filtered_progress', lambda *a, **k: (*next(attempts), '2.2s')), \
         patch('mama.types.git.remove_tree', lambda d: None), \
         patch('mama.types.git.time.sleep', lambda s: None), \
         pytest.raises(GitError) as raised:
        git.clone_with_filtered_progress(dep, '--depth 1 --branch n8.0.1 url', '/packages/ffmpeg')
    return str(raised.value)


def test_the_report_names_the_target_url_ref_dir_command_and_git_error():
    msg = _failed_clone(RESET)
    for expected in ['[CLONE FAILED]  ffmpeg', 'https://git.ffmpeg.org/ffmpeg.git', 'n8.0.1', '/packages/ffmpeg',
                     'git clone --depth 1', '128 after 2.2s', 'curl 35 Recv failure', 'Check the network']:
        assert expected in msg


def test_the_report_drops_the_transfer_chatter():
    assert 'Enumerating objects' not in _failed_clone(RESET)  # the noise that buried the real error


def test_an_unknown_failure_falls_back_to_the_exit_code_and_the_git_output():
    msg = _failed_clone('fatal: the remote end said something new')
    assert 'git exited with code 128' in msg and 'said something new' in msg


def test_a_throttled_clone_reports_how_many_attempts_it_made():
    msg = _failed_clone(THROTTLED)
    assert f'{_CLONE_ATTEMPTS} attempts' in msg and 'rate limit' in msg


def test_a_permanent_failure_reports_the_single_attempt():
    assert 'attempts' not in _failed_clone('ERROR: Repository not found.')  # not retried: a count would be noise


def test_a_failed_fetch_reports_the_git_output_and_the_command():
    # https url: an ssh url makes ensure_master_for_url open a real master connection
    git, dep = make_git_and_mock_dep(url='https://example.com/foo/libfoo.git', git_timeout=300)
    def deny(cmd, cwd=None, io_func=None, idle_timeout=None):
        io_func(None, 'ERROR: Repository not found.')
        return 128
    with patch('mama.types.git.SubProcess.run', deny), pytest.raises(GitError) as raised:
        git.run_git(dep, 'fetch origin main -q')
    msg = str(raised.value)
    assert '[GIT FETCH FAILED]  libfoo' in msg and 'Repository not found' in msg
    # the report names the command as it really ran, scope flags and all, so a user can paste it back
    assert 'fetch origin main -q' in msg and '--git-dir' in msg and 'does not exist' in msg


def test_the_report_prints_red_without_a_traceback(capsys):
    with patch('mama.dependency_chain.error', wraps=dc.error) as red:
        dc._report_error(GitError(_failed_clone(RESET)), verbose=False)
    out = strip_ansi(capsys.readouterr().out)
    assert 'CLONE FAILED' in out and 'curl 35 Recv failure' in out and 'Traceback' not in out
    assert red.called  # error() is the red helper: the report is the one thing the user has to read


def test_a_clone_failure_in_the_load_chain_exits_without_a_traceback(capsys):
    root = make_load_root()
    root.load.side_effect = GitError(_failed_clone(RESET))
    with pytest.raises(SystemExit):
        dc.load_dependency_chain(root)
    out = strip_ansi(capsys.readouterr().out)
    assert 'CLONE FAILED' in out and 'Traceback' not in out
