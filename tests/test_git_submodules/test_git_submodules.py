"""Pins that mama runs a submodule command only for a repo that declares submodules."""
from unittest.mock import patch

from mama.types.git import Git
from testutils import make_git_and_mock_dep


def _git_dep(tmp_path, gitmodules=False):
    git, dep = make_git_and_mock_dep()
    dep.src_dir = str(tmp_path)
    if gitmodules: (tmp_path / '.gitmodules').write_text('[submodule "sub"]\n\tpath = sub\n')
    return git, dep


def test_a_repo_without_submodules_runs_no_command(tmp_path):
    # git charges about 0.9 seconds for `submodule update` even when there is nothing to update
    git, dep = _git_dep(tmp_path)
    with patch.object(Git, 'run_git') as run_git:
        git.update_submodules(dep)
    run_git.assert_not_called()


def test_a_repo_with_submodules_updates_them(tmp_path):
    git, dep = _git_dep(tmp_path, gitmodules=True)
    with patch.object(Git, 'run_git') as run_git:
        git.update_submodules(dep)
    run_git.assert_called_once_with(dep, 'submodule update --init --recursive')


def test_a_shallow_parent_clones_its_submodules_shallow(tmp_path):
    git, dep = _git_dep(tmp_path, gitmodules=True)
    with patch.object(Git, 'run_git') as run_git:
        git.update_submodules(dep, shallow=True)
    run_git.assert_called_once_with(dep, 'submodule update --init --recursive --depth 1')
