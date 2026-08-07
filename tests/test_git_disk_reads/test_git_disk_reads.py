"""Pins the git facts mama reads from disk instead of spawning a process: HEAD and a local tag.
Each one used to cost a git process per dependency per update."""
import os
import pytest

from mama.types.git import read_head, has_local_ref
from testutils import git_run as _git


def _file(repo, name):
    return os.path.join(repo, name)


@pytest.fixture
def repo(source_repo):
    _git(['tag', 'v1.0.0'], source_repo)   # a local tag is the second fact mama reads off disk
    return source_repo


def test_head_reads_the_branch_and_agrees_with_symbolic_ref(repo):
    assert read_head(repo) == 'ref: refs/heads/master'
    assert _git(['symbolic-ref', '-q', 'HEAD'], repo).stdout.strip() == 'refs/heads/master'


def test_head_reads_a_raw_commit_when_detached(repo):
    _git(['checkout', '-q', '--detach', 'HEAD'], repo)
    assert not read_head(repo).startswith('ref: ')
    assert _git(['symbolic-ref', '-q', 'HEAD'], repo).returncode != 0   # git calls it detached too


def test_head_reads_empty_where_only_git_can_answer(tmp_path):
    assert read_head(str(tmp_path / 'nothing')) == ''   # the caller then falls back to the subprocess


def test_a_local_tag_is_found_loose_or_packed(repo):
    assert has_local_ref(repo, 'refs/tags/v1.0.0')
    _git(['pack-refs', '--all'], repo)             # git moves refs into packed-refs on its own schedule
    assert not os.path.isfile(_file(repo, '.git/refs/tags/v1.0.0'))
    assert has_local_ref(repo, 'refs/tags/v1.0.0')


def test_a_tag_this_clone_lacks_is_not_found(repo):
    assert not has_local_ref(repo, 'refs/tags/v9.9.9')
