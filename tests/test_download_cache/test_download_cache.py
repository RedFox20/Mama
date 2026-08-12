"""Size-match cache, target-prefix and failure reporting in download_file."""
import io
import os
import socket
from unittest.mock import patch, MagicMock
from urllib.error import HTTPError, URLError

import pytest

from mama.utils.errors import BuildError
from mama.utils.net import download_file, try_download_file
from mama.utils.paths import normalized_path


def _mock_urlopen(content: bytes, content_length=None):
    """Build a context-manager-returning mock that mimics urllib.request.urlopen."""
    body = io.BytesIO(content)
    cm = MagicMock()
    cm.info.return_value = {'Content-Length': str(content_length if content_length is not None else len(content))}
    cm.read = body.read
    cm.__enter__ = lambda self: cm
    cm.__exit__ = lambda self, *a: None
    return cm


class TestSizeMatchCache:
    def test_skips_body_when_local_size_matches_remote(self, tmp_path):
        cached_path = tmp_path / 'archive.zip'
        cached_path.write_bytes(b'x' * 1024)
        opened = _mock_urlopen(b'NEW' * 100, content_length=1024)
        opened.read = MagicMock(side_effect=AssertionError('body must not be read'))
        with patch('urllib.request.urlopen', return_value=opened):
            assert download_file('http://x.example/archive.zip', str(tmp_path), force=True) == normalized_path(cached_path)
        assert cached_path.read_bytes() == b'x' * 1024

    def test_downloads_when_local_size_differs_from_remote(self, tmp_path):
        cached_path = tmp_path / 'archive.zip'
        cached_path.write_bytes(b'old' * 100)
        new_body = b'NEW' * 200
        opened = _mock_urlopen(new_body, content_length=600)
        with patch('urllib.request.urlopen', return_value=opened):
            assert download_file('http://x.example/archive.zip', str(tmp_path), force=True) == normalized_path(cached_path)
        assert cached_path.read_bytes() == new_body

    def test_downloads_when_no_local_file(self, tmp_path):
        new_body = b'BODY' * 50
        opened = _mock_urlopen(new_body, content_length=200)
        with patch('urllib.request.urlopen', return_value=opened):
            result = download_file('http://x.example/new.zip', str(tmp_path), force=True)
        assert os.path.exists(result)
        assert open(result, 'rb').read() == new_body

    def test_force_false_uses_cache_without_contacting_server(self, tmp_path):
        cached_path = tmp_path / 'a.zip'
        cached_path.write_bytes(b'hello')
        with patch('urllib.request.urlopen', side_effect=AssertionError('must not open URL')):
            assert download_file('http://x.example/a.zip', str(tmp_path), force=False) == normalized_path(cached_path)

    def test_size_match_reported_to_user(self, tmp_path, capsys):
        local_dir = str(tmp_path)
        cached_path = tmp_path / 'archive.zip'
        cached_path.write_bytes(b'x' * 1024)
        opened = _mock_urlopen(b'unused', content_length=1024)

        with patch('urllib.request.urlopen', return_value=opened):
            download_file('http://x.example/archive.zip', local_dir, force=True)
        out = capsys.readouterr().out
        assert 'Artifactory CACHE (size-match)' in out


class TestTargetPrefix:
    def test_name_prefixes_cached_message(self, tmp_path, capsys):
        local_dir = str(tmp_path)
        (tmp_path / 'a.zip').write_bytes(b'x')
        download_file('http://x/a.zip', local_dir, force=False, name='libfoo')
        out = capsys.readouterr().out
        assert 'libfoo' in out
        assert 'Using locally cached' in out

    def test_name_prefixes_size_match_message(self, tmp_path, capsys):
        local_dir = str(tmp_path)
        (tmp_path / 'a.zip').write_bytes(b'x' * 8)
        opened = _mock_urlopen(b'unused', content_length=8)
        with patch('urllib.request.urlopen', return_value=opened):
            download_file('http://x/a.zip', local_dir, force=True, name='libfoo')
        out = capsys.readouterr().out
        assert 'libfoo' in out

    def test_name_prefixes_progress_bar(self, tmp_path, capsys):
        local_dir = str(tmp_path)
        # 200 KB body so report_interval is small enough that progress bars actually print.
        body = b'A' * (200 * 1024)
        opened = _mock_urlopen(body, content_length=len(body))
        with patch('urllib.request.urlopen', return_value=opened):
            download_file('http://x/a.zip', local_dir, force=True, name='libfoo')
        out = capsys.readouterr().out
        # The redrawn progress line must carry the target name, or a parallel run's status lines name no target.
        assert 'libfoo' in out
        assert '|' in out and '%' in out  # the bar format itself is unchanged

    def test_no_name_keeps_unprefixed_format(self, tmp_path, capsys):
        local_dir = str(tmp_path)
        (tmp_path / 'a.zip').write_bytes(b'x')
        download_file('http://x/a.zip', local_dir, force=False)
        out = capsys.readouterr().out
        assert 'Using locally cached' in out
        assert '  - ' not in out  # no target context -> plain 4-space indent, no '- ' bullet


def _mock_urlopen_without_length(content: bytes):
    """A server that sends no Content-Length header. urllib reports the missing key as None."""
    cm = _mock_urlopen(content)
    info = MagicMock()
    info.__getitem__ = lambda self, key: None
    cm.info.return_value = info
    return cm


class TestFailureReport:
    @pytest.mark.parametrize('raised, reason, status, network', [
        (TimeoutError('The read operation timed out'), 'no data for 5 seconds', 0, True),
        (URLError(socket.timeout('timed out')), 'no data for 5 seconds', 0, True),
        (URLError(socket.gaierror(-2, 'Name or service not known')), 'host name does not resolve', 0, True),
        (URLError(ConnectionRefusedError(111, 'Connection refused')), 'ConnectionRefusedError', 0, True),
        (HTTPError('http://x/a.zip', 404, 'Not Found', None, None), 'HTTP 404 Not Found', 404, False),
        (HTTPError('http://x/a.zip', 403, 'Forbidden', None, None), 'HTTP 403 Forbidden', 403, False),
    ])
    def test_every_failure_reports_a_reason_and_raises_nothing(self, tmp_path, raised, reason, status, network):
        with patch('urllib.request.urlopen', side_effect=raised):
            local_file, err = try_download_file('http://x/a.zip', str(tmp_path), force=True)
        assert local_file is None
        assert reason in err.reason and err.status == status and err.network is network
        assert str(err) == f'Failed to download http://x/a.zip: {err.reason}'

    def test_the_reason_names_the_timeout_the_caller_asked_for(self, tmp_path):
        with patch('urllib.request.urlopen', side_effect=TimeoutError('timed out')) as opened:
            _, err = try_download_file('http://x/a.zip', str(tmp_path), force=True, timeout=12)
        assert opened.call_args.kwargs['timeout'] == 12
        assert 'no data for 12 seconds' in err.reason

    def test_a_missing_content_length_is_a_failure(self, tmp_path):
        with patch('urllib.request.urlopen', return_value=_mock_urlopen_without_length(b'body')):
            local_file, err = try_download_file('http://x/a.zip', str(tmp_path), force=True)
        assert local_file is None
        assert 'Content-Length' in err.reason

    def test_a_truncated_body_reports_the_byte_counts_and_leaves_no_file(self, tmp_path):
        opened = _mock_urlopen(b'A' * 300, content_length=1000)
        with patch('urllib.request.urlopen', return_value=opened):
            local_file, err = try_download_file('http://x/a.zip', str(tmp_path), force=True)
        assert local_file is None
        assert 'stopped at 0.3KB of 1.0KB' in err.reason
        assert os.listdir(tmp_path) == []  # a truncated archive must not serve as a cache

    @pytest.mark.parametrize('raised', [TimeoutError('the read timed out'), KeyboardInterrupt('stopped by SIGTERM')])
    def test_a_stopped_transfer_keeps_the_cached_file(self, tmp_path, raised):
        # the artifactory fallback loads that cached zip, and a stop signal raises through the body too
        (tmp_path / 'a.zip').write_bytes(b'cached')
        opened = _mock_urlopen(b'A' * 400, content_length=400)
        opened.read = MagicMock(side_effect=raised)
        with patch('urllib.request.urlopen', return_value=opened):
            try:
                local_file, err = try_download_file('http://x/a.zip', str(tmp_path), force=True)
                assert local_file is None and err is not None
            except KeyboardInterrupt:
                pass  # an interrupt must reach the caller, and the cleanup must still run
        assert os.listdir(tmp_path) == ['a.zip']
        assert (tmp_path / 'a.zip').read_bytes() == b'cached'

    def test_a_failed_connection_keeps_the_cached_file(self, tmp_path):
        (tmp_path / 'a.zip').write_bytes(b'cached')
        with patch('urllib.request.urlopen', side_effect=TimeoutError('timed out')):
            local_file, err = try_download_file('http://x/a.zip', str(tmp_path), force=True)
        assert local_file is None and err is not None
        assert (tmp_path / 'a.zip').read_bytes() == b'cached'

    def test_download_file_raises_build_error_with_the_url_and_the_reason(self, tmp_path):
        with patch('urllib.request.urlopen', side_effect=TimeoutError('timed out')), \
             pytest.raises(BuildError, match='Failed to download http://x/a.zip: .*5 seconds'):
            download_file('http://x/a.zip', str(tmp_path), force=True)
