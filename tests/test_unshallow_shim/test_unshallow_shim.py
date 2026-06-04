"""Pins `mama unshallow <target>`: a cached shim must be dropped so the git path clones source."""
import os
from unittest.mock import patch

from testutils import make_mock_dep, make_mock_shim_dep

import mama.build_dependency as build_dependency
from mama.types.git import Git


def test_force_source_clone_only_for_unshallow_target(tmp_path):
    dep = make_mock_shim_dep(tmp_path, unshallow=True)
    dep.config.target_matches.return_value = True
    assert dep._force_source_clone()
    dep.config.target_matches.return_value = False
    assert not dep._force_source_clone()


def test_unshallow_target_drops_cached_shim(tmp_path):
    dep = make_mock_shim_dep(tmp_path, unshallow=True)
    dep.config.target_matches.return_value = True
    assert dep.is_artifactory_shim()
    assert dep._try_artifactory_shim() is False  # bypasses the cached-shim load
    assert not dep.is_artifactory_shim()          # marker gone -> _git_checkout_if_needed clones


def _load_unshallow_target(dep):
    with patch.object(Git, 'init_commit_hash', return_value='abc1234'), \
         patch.object(Git, 'dependency_checkout', return_value=True) as clone_mock, \
         patch.object(build_dependency, 'artifactory_fetch_and_reconfigure',
                      return_value=(True, [])) as fetch_mock:
        dep.config.target_matches.return_value = True
        dep._load()
    return clone_mock, fetch_mock


def test_unshallow_converts_shim_to_clone_without_refetch(tmp_path):
    # papa.txt is the shim leftover that makes the post-clone probe think a pkg is available.
    dep = make_mock_shim_dep(tmp_path, unshallow=True, write_papa_txt=True)
    assert dep.is_artifactory_shim()
    clone_mock, fetch_mock = _load_unshallow_target(dep)
    clone_mock.assert_called_once()
    fetch_mock.assert_not_called()    # the bug: post-clone probe re-fetched the pkg, making the clone moot
    assert not dep.is_artifactory_shim()
    assert not dep.from_artifactory


def test_unshallow_already_clone_target_skips_artifactory(tmp_path):
    # No shim marker: the target is already a real clone, re-unshallowed. Must still prefer source.
    dep = make_mock_dep(tmp_path, unshallow=True)
    (tmp_path / 'packages/libfoo/linux/papa.txt').write_text('p libfoo\nv 1.0\n')
    assert not dep.is_artifactory_shim()
    _, fetch_mock = _load_unshallow_target(dep)
    fetch_mock.assert_not_called()
    assert not dep.from_artifactory


def test_limbo_dir_is_wiped_and_recloned(tmp_path):
    # A dropped shim leaves a non-.git dir (e.g. mama.cmake proxy): it can't be pulled, must reclone.
    dep = make_mock_dep(tmp_path)
    os.makedirs(dep.src_dir, exist_ok=True)
    with open(f'{dep.src_dir}/mama.cmake', 'w') as f: f.write('# proxy stub\n')
    assert dep.source_dir_exists() and not dep.is_real_clone()
    with patch.object(Git, 'reclone_wipe') as wipe_mock, \
         patch.object(Git, 'clone_or_pull') as clone_mock, \
         patch.object(Git, '_sync_remote_url') as sync_mock:
        dep.dep_source.dependency_checkout(dep)
    wipe_mock.assert_called_once()
    clone_mock.assert_called_once()
    sync_mock.assert_not_called()    # must NOT take the pull-an-existing-repo path
