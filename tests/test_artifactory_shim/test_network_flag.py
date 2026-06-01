"""Reactive network-availability flag: classification + caching."""
import socket
import subprocess
from unittest.mock import Mock
from urllib.error import URLError, HTTPError

from mama.util import is_network_error
from mama.build_config import BuildConfig


def test_timeout_is_network_error():
    e = subprocess.TimeoutExpired(cmd='git ls-remote', timeout=5)
    assert is_network_error(e) is True


def test_connection_refused_is_network_error():
    assert is_network_error(ConnectionRefusedError()) is True


def test_socket_timeout_is_network_error():
    assert is_network_error(socket.timeout('timed out')) is True


def test_dns_failure_is_network_error():
    assert is_network_error(socket.gaierror('Name or service not known')) is True


def test_urlerror_with_socket_reason_is_network_error():
    e = URLError(reason=socket.timeout('timed out'))
    assert is_network_error(e) is True


def test_http_401_is_not_network_error():
    e = HTTPError(url='http://x', code=401, msg='Unauthorized', hdrs=None, fp=None)
    assert is_network_error(e) is False


def test_http_403_is_not_network_error():
    e = HTTPError(url='http://x', code=403, msg='Forbidden', hdrs=None, fp=None)
    assert is_network_error(e) is False


def test_http_404_is_not_network_error():
    e = HTTPError(url='http://x', code=404, msg='Not Found', hdrs=None, fp=None)
    assert is_network_error(e) is False


def test_permission_denied_in_message_is_not_network_error():
    e = RuntimeError('fatal: Permission denied (publickey)')
    assert is_network_error(e) is False


def test_host_key_verification_failed_is_not_network_error():
    e = RuntimeError('Host key verification failed.')
    assert is_network_error(e) is False


def test_connection_timed_out_in_message_is_network_error():
    e = RuntimeError('ssh: connect to host github.com: Connection timed out')
    assert is_network_error(e) is True


def test_could_not_resolve_host_is_network_error():
    e = RuntimeError("fatal: unable to access: Could not resolve host: github.com")
    assert is_network_error(e) is True


def test_ambiguous_error_is_not_network_error():
    e = RuntimeError('something unexpected happened')
    assert is_network_error(e) is False


def test_config_network_available_by_default():
    config = BuildConfig(['build'])
    assert config.is_network_available() is True


def test_config_mark_network_unavailable_sticks():
    config = BuildConfig(['build'])
    config.print = False
    config.mark_network_unavailable()
    assert config.is_network_available() is False


def test_config_mark_network_unavailable_is_idempotent():
    config = BuildConfig(['build'])
    config.print = False
    config.mark_network_unavailable()
    config.mark_network_unavailable()  # no crash, no duplicate messages
    assert config.is_network_available() is False
