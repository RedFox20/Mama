"""Pins reading the current commit from `.git` instead of spawning `git show --format=%h`. The length
comes from what mama stored last time, so an archive name cannot move."""
import os, subprocess
import pytest

from mama.types.git import read_commit_from_disk


def _git(args, cwd):
    return subprocess.run(['git', *args], cwd=str(cwd), capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path):
    d = tmp_path / 'dep'; d.mkdir()
    for cmd in ('init -q -b master', 'config user.email t@t', 'config user.name t'):
        _git(cmd.split(), d)
    (d / 'lib.cpp').write_text('int f(){return 1;}\n')
    _git(['add', '-A'], d); _git(['commit', '-q', '-m', 'init'], d)
    return str(d)


def _git_says(repo) -> str:
    return _git(['show', '--format=%h', '-s'], repo).stdout.strip()


def test_the_disk_read_matches_what_git_reports(repo):
    assert read_commit_from_disk(repo, len(_git_says(repo))) == _git_says(repo)


def test_a_packed_ref_resolves_too(repo):
    _git(['pack-refs', '--all'], repo)   # git packs refs on its own schedule, so both shapes must work
    assert not os.path.isfile(os.path.join(repo, '.git', 'refs', 'heads', 'master'))
    assert read_commit_from_disk(repo, len(_git_says(repo))) == _git_says(repo)


def test_a_detached_head_reads_the_object_name(repo):
    _git(['checkout', '-q', '--detach', 'HEAD'], repo)
    assert read_commit_from_disk(repo, len(_git_says(repo))) == _git_says(repo)


def test_a_dirty_tree_reports_the_same_commit(repo):
    # a modified working tree does NOT change the abbreviation
    before = read_commit_from_disk(repo, 7)
    open(os.path.join(repo, 'scratch.txt'), 'w').write('x\n')
    assert read_commit_from_disk(repo, 7) == before == _git_says(repo)


def test_the_stored_length_decides_the_shape(repo):
    # mama recorded 7 characters, so it keeps producing 7 even if git would grow to 8
    assert len(read_commit_from_disk(repo, 7)) == 7
    assert len(read_commit_from_disk(repo, 10)) == 10


def test_a_shape_disk_cannot_settle_answers_empty(tmp_path):
    assert read_commit_from_disk(str(tmp_path / 'nothing'), 7) == ''   # the caller then asks git
