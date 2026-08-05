"""Pins `source_walk_moved`, the cheap gate in front of the git check. An inverted answer here skips
the git check, so a real edit never rebuilds."""
import os
from unittest.mock import patch
import pytest

from mama.utils import git_status as util


@pytest.fixture
def tree(tmp_path):
    src = tmp_path / 'src'; src.mkdir()
    (src / 'lib.cpp').write_text('int f(){return 1;}\n')
    build = tmp_path / 'build'; build.mkdir()
    with patch.object(util.System, 'windows', True):
        yield str(src), str(build)


def test_no_record_means_ask_git(tree):
    src, build = tree
    assert not os.path.isfile(util.source_walk_file(build))
    assert util.source_walk_moved(src, build) is True


def test_a_recorded_walk_closes_the_gate(tree):
    src, build = tree
    util.record_source_walk(src, build)
    assert util.source_walk_moved(src, build) is False


def test_an_edited_source_opens_the_gate(tree):
    src, build = tree
    util.record_source_walk(src, build)
    open(os.path.join(src, 'lib.cpp'), 'w').write('int f(){return 2;}\nint g(){return 3;}\n')
    assert util.source_walk_moved(src, build) is True


def test_an_edited_readme_leaves_it_closed(tree):
    src, build = tree
    util.record_source_walk(src, build)
    open(os.path.join(src, 'README.md'), 'w').write('# docs\n')
    assert util.source_walk_moved(src, build) is False


def test_off_windows_the_gate_never_closes(tree):
    src, build = tree
    util.record_source_walk(src, build)
    with patch.object(util.System, 'windows', False):
        assert util.source_walk_moved(src, build) is True   # git status is already fast on ext4
