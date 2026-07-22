"""Pins broken-.git auto-heal: a corrupt/half-cloned tree is wiped and re-cloned, not pulled."""
import os
from unittest.mock import patch
from testutils import make_mock_dep


def _fake_clone(dep):
    os.makedirs(f'{dep.src_dir}/.git', exist_ok=True)  # looks like a real clone to is_real_clone()


def test_is_repo_broken_true_when_head_unresolvable(tmp_path):
    dep = make_mock_dep(tmp_path)
    with patch('mama.types.git.execute_piped', return_value=''):  # rev-parse -q printed nothing -> broken
        assert dep.dep_source._is_repo_broken(dep)


def test_is_repo_broken_false_for_healthy_head(tmp_path):
    dep = make_mock_dep(tmp_path)
    with patch('mama.types.git.execute_piped', return_value='a1b2c3d'):
        assert not dep.dep_source._is_repo_broken(dep)


def test_checkout_heals_a_broken_git_dir(tmp_path):
    dep = make_mock_dep(tmp_path)
    _fake_clone(dep)  # is_real_clone() True, but the repo is corrupt (HEAD unresolvable)
    git = dep.dep_source
    with patch.object(git, '_is_repo_broken', return_value=True), \
         patch.object(git, 'reclone_wipe') as wipe, patch.object(git, 'clone_or_pull') as clone:
        assert git.dependency_checkout(dep) is True
    wipe.assert_called_once()   # corrupt tree wiped...
    clone.assert_called_once()  # ...and re-cloned fresh


def test_checkout_leaves_a_healthy_clone_alone(tmp_path):
    dep = make_mock_dep(tmp_path)
    _fake_clone(dep)
    dep.config.reclone = False
    dep.is_current_target = lambda: False  # non-target dep of a plain build: no changes -> no pull/wipe
    git = dep.dep_source
    with patch.object(git, '_is_repo_broken', return_value=False), patch.object(git, '_sync_remote_url'), \
         patch.object(git, 'reclone_wipe') as wipe, patch.object(git, 'clone_or_pull') as clone:
        git.dependency_checkout(dep)
    wipe.assert_not_called()
    clone.assert_not_called()
