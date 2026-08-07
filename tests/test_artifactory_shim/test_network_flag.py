"""Reactive network-availability flag: which failures mean `the network is gone`, and the sticky config flag."""
import socket
import subprocess
from urllib.error import URLError, HTTPError

import pytest

from mama.utils.net import is_network_error
from mama.build_config import BuildConfig


@pytest.mark.parametrize('error', [
    subprocess.TimeoutExpired(cmd='git ls-remote', timeout=5),
    ConnectionRefusedError(),
    socket.timeout('timed out'),
    socket.gaierror('Name or service not known'),
    URLError(reason=socket.timeout('timed out')),
    RuntimeError('ssh: connect to host github.com: Connection timed out'),
    RuntimeError('fatal: unable to access: Could not resolve host: github.com'),
], ids=lambda e: type(e).__name__ + ':' + str(e)[:40])
def test_a_transport_failure_marks_the_network_unavailable(error):
    assert is_network_error(error) is True


@pytest.mark.parametrize('error', [
    HTTPError(url='http://x', code=401, msg='Unauthorized', hdrs=None, fp=None),
    HTTPError(url='http://x', code=403, msg='Forbidden', hdrs=None, fp=None),
    HTTPError(url='http://x', code=404, msg='Not Found', hdrs=None, fp=None),
    RuntimeError('fatal: Permission denied (publickey)'),
    RuntimeError('Host key verification failed.'),
    RuntimeError('something unexpected happened'),   # ambiguous, so never assume the network is gone
], ids=lambda e: type(e).__name__ + ':' + str(e)[:40])
def test_the_server_answering_is_never_a_network_error(error):
    # the server replied, so the network works. Marking it down would skip every later fetch of the run.
    assert is_network_error(error) is False


def test_the_flag_starts_available_and_sticks_once_it_goes_down():
    config = BuildConfig(['build'])
    config.print = False
    assert config.is_network_available() is True
    config.mark_network_unavailable()
    config.mark_network_unavailable()   # idempotent: no crash, no duplicate message
    assert config.is_network_available() is False
