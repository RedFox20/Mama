"""Pins the update guard: a dirty working tree fails `mama update` with a clear error (marked `x`)
even when upstream is unchanged. A swallowed pull error left the dep silently un-updated."""
import subprocess
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from testutils import git_init_commit, make_mock_dep

from mama.build_dependency import BuildDependency
from mama.types.git import Git


def test_untracked_file_blocks_locking_but_not_an_unlocked_update(tmp_path):
    dep = make_mock_dep(tmp_path)
    dep.config.git_timeout = 30
    git_init_commit(dep.src_dir, files={'source.cpp': 'int source;\n'})

    assert not dep.dep_source._has_local_modifications(dep)
    dep.dep_source.locked_commit = 'a' * 40
    assert not dep.dep_source._has_local_modifications(dep)
    dep.dep_source.locked_commit = ''
    Path(dep.src_dir, 'generated.h').write_text('// generated\n', encoding='utf-8')
    dep.config.lock_generation = True
    assert dep.dep_source._has_local_modifications(dep)
    dep.config.lock_generation = False
    dep.dep_source.locked_commit = 'a' * 40
    assert dep.dep_source._has_local_modifications(dep)


def test_failed_locked_status_check_is_not_clean(tmp_path):
    dep = make_mock_dep(tmp_path)
    dep.dep_source.locked_commit = 'a' * 40
    with patch('mama.types.git.subprocess.run', return_value=Mock(returncode=128, stdout=b'')):
        assert dep.dep_source._has_local_modifications(dep)


def test_locked_status_ignores_stderr_warnings(tmp_path):
    dep = make_mock_dep(tmp_path)
    dep.dep_source.locked_commit = 'a' * 40
    with patch('mama.types.git.subprocess.run', return_value=Mock(returncode=0, stdout=b'')) as run:
        assert not dep.dep_source._has_local_modifications(dep)
    assert run.call_args.kwargs['stderr'] == subprocess.DEVNULL


def test_ensure_no_local_modifications_raises_actionable(tmp_path):
    dep = make_mock_dep(tmp_path)
    with patch.object(Git, '_has_local_modifications', return_value=True), \
         patch.object(Git, 'run_git') as run_git:   # the `git status --porcelain` it shows the user
        with pytest.raises(RuntimeError, match='mama wipe'):
            dep.dep_source._ensure_no_local_modifications(dep)
    run_git.assert_called_once()


def test_update_fails_on_dirty_tree_even_without_upstream_change(tmp_path):
    dep = make_mock_dep(tmp_path, update=True)
    dep.config.target_matches.return_value = True
    with patch.object(BuildDependency, 'is_real_clone', return_value=True), \
         patch.object(Git, '_is_repo_broken', return_value=False), \
         patch.object(Git, '_sync_remote_url'), \
         patch.object(Git, '_has_local_modifications', return_value=True), \
         patch.object(Git, 'run_git'), \
         patch.object(Git, 'check_status') as check_status:
        with pytest.raises(RuntimeError, match='local modifications'):
            dep.dep_source.dependency_checkout(dep)
    check_status.assert_not_called()   # failed BEFORE the pull whose error the fetch fallback would swallow
