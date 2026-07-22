"""Pins the update guard: a dirty working tree fails `mama update` loudly (marked `x`) even when
upstream is unchanged, instead of a swallowed pull error leaving the dep silently un-updated."""
from unittest.mock import patch

import pytest
from testutils import make_mock_dep

from mama.build_dependency import BuildDependency
from mama.types.git import Git


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
