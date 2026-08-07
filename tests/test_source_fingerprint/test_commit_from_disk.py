"""Pins reading the current commit from `.git` instead of spawning `git show --format=%h`. The length
comes from what mama stored last time, so an archive name cannot move."""
import os

from mama.types.git import read_commit_from_disk
from testutils import git_run as _git


def _git_says(source_repo) -> str:
    return _git(['show', '--format=%h', '-s'], source_repo).stdout.strip()


def test_the_disk_read_matches_what_git_reports(source_repo):
    assert read_commit_from_disk(source_repo, len(_git_says(source_repo))) == _git_says(source_repo)


def test_a_packed_ref_resolves_too(source_repo):
    _git(['pack-refs', '--all'], source_repo)   # git packs refs on its own schedule, so both shapes must work
    assert not os.path.isfile(os.path.join(source_repo, '.git', 'refs', 'heads', 'master'))
    assert read_commit_from_disk(source_repo, len(_git_says(source_repo))) == _git_says(source_repo)


def test_a_detached_head_reads_the_object_name(source_repo):
    _git(['checkout', '-q', '--detach', 'HEAD'], source_repo)
    assert read_commit_from_disk(source_repo, len(_git_says(source_repo))) == _git_says(source_repo)


def test_a_dirty_tree_reports_the_same_commit(source_repo):
    # a modified working tree does NOT change the abbreviation
    before = read_commit_from_disk(source_repo, 7)
    open(os.path.join(source_repo, 'scratch.txt'), 'w').write('x\n')
    assert read_commit_from_disk(source_repo, 7) == before == _git_says(source_repo)


def test_the_stored_length_decides_the_shape(source_repo):
    # mama recorded 7 characters, so it keeps producing 7 even if git would grow to 8
    assert len(read_commit_from_disk(source_repo, 7)) == 7
    assert len(read_commit_from_disk(source_repo, 10)) == 10


def test_a_shape_disk_cannot_settle_answers_empty(tmp_path):
    assert read_commit_from_disk(str(tmp_path / 'nothing'), 7) == ''   # the caller then asks git
