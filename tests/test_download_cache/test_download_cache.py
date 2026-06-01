"""Size-match cache + target-prefix in download_file."""
import io
import os
from unittest.mock import patch, MagicMock

import pytest

from mama.util import download_file


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
        with patch('mama.util.request.urlopen', return_value=opened):
            assert download_file('http://x.example/archive.zip', str(tmp_path), force=True) == str(cached_path)
        assert cached_path.read_bytes() == b'x' * 1024

    def test_downloads_when_local_size_differs_from_remote(self, tmp_path):
        cached_path = tmp_path / 'archive.zip'
        cached_path.write_bytes(b'old' * 100)
        new_body = b'NEW' * 200
        opened = _mock_urlopen(new_body, content_length=600)
        with patch('mama.util.request.urlopen', return_value=opened):
            assert download_file('http://x.example/archive.zip', str(tmp_path), force=True) == str(cached_path)
        assert cached_path.read_bytes() == new_body

    def test_downloads_when_no_local_file(self, tmp_path):
        new_body = b'BODY' * 50
        opened = _mock_urlopen(new_body, content_length=200)
        with patch('mama.util.request.urlopen', return_value=opened):
            result = download_file('http://x.example/new.zip', str(tmp_path), force=True)
        assert os.path.exists(result)
        assert open(result, 'rb').read() == new_body

    def test_force_false_uses_cache_without_contacting_server(self, tmp_path):
        cached_path = tmp_path / 'a.zip'
        cached_path.write_bytes(b'hello')
        with patch('mama.util.request.urlopen', side_effect=AssertionError('must not open URL')):
            assert download_file('http://x.example/a.zip', str(tmp_path), force=False) == str(cached_path)

    def test_size_match_reported_to_user(self, tmp_path, capsys):
        local_dir = str(tmp_path)
        cached_path = tmp_path / 'archive.zip'
        cached_path.write_bytes(b'x' * 1024)
        opened = _mock_urlopen(b'unused', content_length=1024)

        with patch('mama.util.request.urlopen', return_value=opened):
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
        with patch('mama.util.request.urlopen', return_value=opened):
            download_file('http://x/a.zip', local_dir, force=True, name='libfoo')
        out = capsys.readouterr().out
        assert 'libfoo' in out

    def test_name_prefixes_progress_bar(self, tmp_path, capsys):
        local_dir = str(tmp_path)
        # 200 KB body so report_interval is small enough that progress bars actually print.
        body = b'A' * (200 * 1024)
        opened = _mock_urlopen(body, content_length=len(body))
        with patch('mama.util.request.urlopen', return_value=opened):
            download_file('http://x/a.zip', local_dir, force=True, name='libfoo')
        out = capsys.readouterr().out
        # The redrawn progress line must carry the target name, otherwise a
        # parallel run's status lines have no way to indicate which target
        # they belong to.
        assert 'libfoo' in out
        # And the existing bar format is preserved.
        assert '|' in out and '%' in out

    def test_no_name_keeps_unprefixed_format(self, tmp_path, capsys):
        local_dir = str(tmp_path)
        (tmp_path / 'a.zip').write_bytes(b'x')
        download_file('http://x/a.zip', local_dir, force=False)
        out = capsys.readouterr().out
        # When no name is given, the line uses plain 4-space indent (no '- ').
        assert 'Using locally cached' in out
        # Must not produce a "- " bullet prefix when there's no target context.
        assert '  - ' not in out
