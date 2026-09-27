"""Pins the two path joins: path_join keeps a path where it points, normalized_join makes it absolute."""
import ntpath, os
from unittest.mock import patch

import pytest

from mama.utils import paths
from mama.utils.paths import normalized_join, normalized_path, path_join


@pytest.mark.parametrize('parts,joined', [
    (('a', 'b'), 'a/b'),
    (('a/', '/b'), 'a/b'),          # one separator, whoever supplied it
    (('', 'b'), 'b'),
    (('a', ''), 'a'),
    (('/a', 'b'), '/a/b'),          # an absolute path stays absolute
    (('a', 'b/'), 'a/b/'),          # a trailing separator is the caller's choice
    (('a', 'b', 'c'), 'a/b/c'),     # N parts, like normalized_join
    (('a/', 'b/', 'c'), 'a/b/c'),
])
def test_path_join(parts, joined):
    assert path_join(*parts) == joined


def test_path_join_keeps_a_relative_path_relative(tmp_path, monkeypatch):
    # normalized_join resolves against the working dir, and mama changes it during a build
    monkeypatch.chdir(tmp_path)
    assert path_join('build', 'out.txt') == 'build/out.txt'
    assert normalized_join('build', 'out.txt') != 'build/out.txt'


@pytest.mark.parametrize('path, normalized', [
    ('c:\\dev\\repro', 'C:/dev/repro'),
    ('C:/dev/repro', 'C:/dev/repro'),
    ('\\\\?\\c:\\dev\\repro', '//?/C:/dev/repro'),
    ('\\\\server\\share\\x', '//server/share/x'),
    ('\\\\?\\unc\\server\\share\\x', '//?/unc/server/share/x'),
])
def test_normalized_path_upper_cases_the_windows_drive(path, normalized):
    # an IDE terminal starts in c:, a shell in C:, and each spelling rewrote every generated cmake file
    with patch.object(paths.System, 'windows', True), patch.object(paths.os.path, 'abspath', ntpath.abspath):
        assert normalized_path(path) == normalized


@pytest.mark.skipif(os.name != 'nt', reason='the drive letter only appears on Windows')
def test_path_join_keeps_a_posix_path_off_the_current_drive():
    # normalized_join('/tmp', 'x') answers 'C:/tmp/x', which is not the path an ssh option meant
    assert path_join('/tmp', 'mama-cm') == '/tmp/mama-cm'
    assert normalized_join('/tmp', 'mama-cm').endswith(':/tmp/mama-cm')
