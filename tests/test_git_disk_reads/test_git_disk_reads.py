"""Pins the git facts mama reads from disk instead of spawning a process: HEAD and a local tag.
Each one used to cost a git process per dependency per update."""
import os, subprocess
import pytest

from mama.types.git import read_head, has_local_ref


def _git(args, cwd):
    return subprocess.run(['git', *args], cwd=str(cwd), capture_output=True, text=True)


def _file(repo, name):
    return os.path.join(repo, name)


@pytest.fixture
def repo(tmp_path):
    d = tmp_path / 'dep'
    d.mkdir()
    _git(['init', '-q', '-b', 'master'], d)
    _git(['config', 'user.email', 't@t'], d); _git(['config', 'user.name', 't'], d)
    (d / 'lib.cpp').write_text('int f(){return 1;}\n')
    _git(['add', '-A'], d); _git(['commit', '-q', '-m', 'init'], d)
    _git(['tag', 'v1.0.0'], d)
    return str(d)


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
