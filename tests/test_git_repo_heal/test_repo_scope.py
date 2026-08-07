"""Pins that a git command mama runs for a dependency can never reach the repository above it.

A corrupt `.git` makes git resume its discovery walk UPWARD. A `reset --hard` in the dependency then
lands on the enclosing checkout of the developer and destroys uncommitted work.
"""
import os
import pytest

from testutils import make_mock_dep, git_init_commit


PRECIOUS = 'PRECIOUS UNCOMMITTED EDIT\n'


def _repo_with_dep(tmp_path, break_dep_git):
    """A parent repo holding a dependency clone, plus an uncommitted edit to a TRACKED parent file.
    `reset --hard` reverts a tracked edit and leaves an untracked file alone, so only this shape shows
    the damage. Returns (dep, the parent file to read back)."""
    parent = tmp_path / 'parent'
    (parent / 'dep').mkdir(parents=True)
    (parent / 'tracked.txt').write_text('committed\n')
    (parent / 'dep' / 'lib.cpp').write_text('int f(){return 1;}\n')
    git_init_commit(parent)

    dep = make_mock_dep(tmp_path, name='dep')
    dep.src_dir = str(parent / 'dep')
    git_init_commit(dep.src_dir)
    if break_dep_git:
        for name in os.listdir(f'{dep.src_dir}/.git'):
            path = f'{dep.src_dir}/.git/{name}'
            if os.path.isfile(path): os.remove(path)  # keeps .git present, but git no longer accepts it
    (parent / 'tracked.txt').write_text(PRECIOUS)
    return dep, parent / 'tracked.txt'


def test_a_reset_in_a_broken_dep_never_touches_the_parent_repo(tmp_path):
    dep, parent_file = _repo_with_dep(tmp_path, break_dep_git=True)
    dep.dep_source.run_git(dep, 'reset --hard', throw=False)
    assert parent_file.read_text() == PRECIOUS


def test_a_reset_in_a_healthy_dep_still_works(tmp_path):
    dep, parent_file = _repo_with_dep(tmp_path, break_dep_git=False)
    edited = f'{dep.src_dir}/lib.cpp'
    with open(edited, 'w') as f: f.write('int f(){return 2;}\n')
    assert dep.dep_source.run_git(dep, 'reset --hard') == 0
    assert 'return 1' in open(edited).read()   # the dependency reverted to its own commit
    assert parent_file.read_text() == PRECIOUS  # and the parent kept its edit


def test_a_destructive_command_refuses_on_a_broken_repo(tmp_path):
    dep, _ = _repo_with_dep(tmp_path, break_dep_git=True)
    assert dep.dep_source.run_git(dep, 'checkout master', throw=False) != 0


@pytest.mark.parametrize('command', ['status --porcelain', 'diff --quiet HEAD'])
def test_a_read_only_command_still_runs_on_a_healthy_repo(tmp_path, command):
    dep, _ = _repo_with_dep(tmp_path, break_dep_git=False)
    assert dep.dep_source.run_git(dep, command, throw=False) == 0
