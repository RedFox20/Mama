"""End-to-end of the shim probe path through BuildDependency._load()."""
import os
from unittest.mock import patch

from testutils import make_mock_dep

import mama.artifactory as artifactory_mod
from mama.types.git import Git


def _fake_successful_fetch(probe_target):
    probe_target.dep.from_artifactory = True
    probe_target.exported_includes = ['/fake/include']
    return (True, [])


def _fake_failed_fetch(probe_target):
    return (False, None)


def test_load_uses_shim_and_skips_clone(tmp_path):
    dep = make_mock_dep(tmp_path)
    with patch.object(Git, 'init_commit_hash', return_value='abc1234'), \
         patch.object(artifactory_mod, 'artifactory_fetch_and_reconfigure', side_effect=_fake_successful_fetch), \
         patch.object(Git, 'dependency_checkout') as clone_mock:
        dep._load()
    clone_mock.assert_not_called()
    assert dep.from_artifactory is True
    assert os.path.exists(dep.mama_shim_file())
    assert dep.is_artifactory_shim()


def test_load_falls_back_to_clone_on_shim_miss(tmp_path):
    dep = make_mock_dep(tmp_path)
    with patch.object(Git, 'init_commit_hash', return_value='abc1234'), \
         patch.object(artifactory_mod, 'artifactory_fetch_and_reconfigure', side_effect=_fake_failed_fetch), \
         patch.object(Git, 'dependency_checkout', return_value=False) as clone_mock:
        dep._load()
    clone_mock.assert_called_once()
    assert not dep.from_artifactory
    assert not os.path.exists(dep.mama_shim_file())


def test_load_skips_shim_when_noart_flag_set(tmp_path):
    dep = make_mock_dep(tmp_path, disable_artifactory=True)
    with patch.object(Git, 'init_commit_hash') as hash_mock, \
         patch.object(artifactory_mod, 'artifactory_fetch_and_reconfigure') as fetch_mock, \
         patch.object(Git, 'dependency_checkout', return_value=False) as clone_mock:
        dep._load()
    hash_mock.assert_not_called()
    fetch_mock.assert_not_called()
    clone_mock.assert_called_once()
    assert not os.path.exists(dep.mama_shim_file())


def test_load_sets_did_check_artifactory_on_shim_hit(tmp_path):
    # A hit MUST suppress the post-clone probe to avoid a redundant artifactory round-trip.
    dep = make_mock_dep(tmp_path)
    with patch.object(Git, 'init_commit_hash', return_value='abc1234'), \
         patch.object(artifactory_mod, 'artifactory_fetch_and_reconfigure', side_effect=_fake_successful_fetch), \
         patch.object(Git, 'dependency_checkout') as clone_mock:
        dep._load()
    assert dep.did_check_artifactory is True
    clone_mock.assert_not_called()
