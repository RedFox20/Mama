"""Pins that mama runs one submodule init per repo that declares submodules, minus .worktrees."""
from unittest.mock import patch

from mama.types.git import Git
from mama.utils.sub_process import execute_piped
from testutils import git_init_commit, make_git_and_mock_dep

# a committed worktree dir is a gitlink with no .gitmodules url, and one undeclared gitlink
# fails the whole init, so the exclude must ride on every submodule update
EXCLUDE = '-- ":(exclude).worktrees"'


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
    run_git.assert_called_once_with(dep, f'submodule update --init --recursive {EXCLUDE}')


def test_a_shallow_parent_clones_its_submodules_shallow(tmp_path):
    git, dep = _git_dep(tmp_path, gitmodules=True)
    with patch.object(Git, 'run_git') as run_git:
        git.update_submodules(dep, shallow=True)
    run_git.assert_called_once_with(dep, f'submodule update --init --recursive --depth 1 {EXCLUDE}')


def test_git_itself_skips_a_stale_worktree_gitlink(tmp_path, monkeypatch):
    # pins the pathspec contract with the real git: the exclude must skip the undeclared gitlink
    for key, value in (('GIT_CONFIG_COUNT', '1'), ('GIT_CONFIG_KEY_0', 'protocol.file.allow'),
                       ('GIT_CONFIG_VALUE_0', 'always')):
        monkeypatch.setenv(key, value)
    sub = tmp_path / 'subrepo'
    sub.mkdir(); (sub / 'f').write_text('hi\n')
    git_init_commit(sub)
    main = tmp_path / 'main'; main.mkdir()
    run = lambda *a: execute_piped(['git', *a], cwd=str(main))
    run('init', '-q')
    run('submodule', 'add', '-q', str(sub), 'sub')
    run('update-index', '--add', '--cacheinfo', f'160000,{"1" * 40},.worktrees/diag')
    run('commit', '-qm', 'sub plus stale gitlink')
    clone = tmp_path / 'clone'
    execute_piped(['git', 'clone', '-q', str(main), str(clone)])

    git, dep = make_git_and_mock_dep(url=str(main), git_timeout=60)
    dep.src_dir = str(clone)
    git.update_submodules(dep)
    assert (clone / 'sub' / 'f').exists()
