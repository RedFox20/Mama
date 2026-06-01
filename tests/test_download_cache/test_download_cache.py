"""Tests for the size-match cache and target-prefix in download_file.

Background: ``mama update`` re-fetches every artifactory archive on each run
because ``_fetch_package`` passes ``force=True`` to ``download_file``. The
size-match cache lets us still skip the body transfer when the local file's
size matches the remote's Content-Length, costing only the HTTP round-trip
we'd open anyway. The ``name`` parameter prefixes every log line with the
target name so parallel updates produce readable output instead of progress
bars from one target glued to status lines from another.
"""
from __future__ import annotations

import io
import os
import sys
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))
from mama.util import download_file  # noqa: E402


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
    def test_skips_body_when_local_size_matches_remote(self, tmp_path, capsys):
        """Saves the body transfer for an already-downloaded artifactory archive."""
        local_dir = str(tmp_path)
        # Pre-populate a file at the URL's basename with known size.
        cached_path = tmp_path / 'archive.zip'
        cached_path.write_bytes(b'x' * 1024)

        # Server says 1024 bytes - same as local. download_file should not
        # read any bytes from the body.
        opened = _mock_urlopen(b'NEW' * 100, content_length=1024)
        opened.read = MagicMock(side_effect=AssertionError('body should not be read'))

        with patch('mama.util.request.urlopen', return_value=opened):
            result = download_file('http://x.example/archive.zip', local_dir, force=True)
        assert result == str(cached_path)
        # Cached file still has the original contents - body was not touched.
        assert cached_path.read_bytes() == b'x' * 1024

    def test_downloads_when_local_size_differs_from_remote(self, tmp_path):
        local_dir = str(tmp_path)
        cached_path = tmp_path / 'archive.zip'
        cached_path.write_bytes(b'old' * 100)  # 300 bytes locally
        new_body = b'NEW' * 200  # 600 bytes from server
        opened = _mock_urlopen(new_body, content_length=600)

        with patch('mama.util.request.urlopen', return_value=opened):
            result = download_file('http://x.example/archive.zip', local_dir, force=True)
        assert result == str(cached_path)
        # File was actually re-downloaded with new contents.
        assert cached_path.read_bytes() == new_body

    def test_downloads_when_no_local_file(self, tmp_path):
        local_dir = str(tmp_path)
        new_body = b'BODY' * 50
        opened = _mock_urlopen(new_body, content_length=200)

        with patch('mama.util.request.urlopen', return_value=opened):
            result = download_file('http://x.example/new.zip', local_dir, force=True)
        assert os.path.exists(result)
        assert open(result, 'rb').read() == new_body

    def test_force_false_uses_cache_without_contacting_server(self, tmp_path):
        """With force=False the function must not touch the network at all."""
        local_dir = str(tmp_path)
        cached_path = tmp_path / 'a.zip'
        cached_path.write_bytes(b'hello')

        with patch('mama.util.request.urlopen', side_effect=AssertionError('must not open URL')):
            result = download_file('http://x.example/a.zip', local_dir, force=False)
        assert result == str(cached_path)

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
