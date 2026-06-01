"""try_load_artifactory_shim probe + gating without contacting any real server."""
import os
from unittest.mock import patch

from testutils import make_mock_dep

import mama.artifactory as artifactory_mod
from mama.artifactory import try_load_artifactory_shim
from mama.types.git import Git


def test_no_artifactory_returns_none(tmp_path):
    dep = make_mock_dep(tmp_path, artifactory_ftp=None)
    target, deps = try_load_artifactory_shim(dep)
    assert target is None and deps is None
    assert not os.path.exists(dep.mama_shim_file())


def test_unresolvable_hash_returns_none(tmp_path):
    dep = make_mock_dep(tmp_path)
    with patch.object(Git, 'init_commit_hash', return_value=None):
        target, deps = try_load_artifactory_shim(dep)
    assert target is None and deps is None
    assert not os.path.exists(dep.mama_shim_file())
    assert not dep.from_artifactory


def test_fetch_fails_clears_state(tmp_path):
    # from_artifactory must be reset on a fetch miss so the caller's clone path runs cleanly.
    dep = make_mock_dep(tmp_path)
    with patch.object(Git, 'init_commit_hash', return_value='abc1234'), \
         patch.object(artifactory_mod, 'artifactory_fetch_and_reconfigure', return_value=(False, None)):
        target, deps = try_load_artifactory_shim(dep)
    assert target is None and deps is None
    assert not os.path.exists(dep.mama_shim_file())
    assert not dep.from_artifactory


def test_fetch_succeeds_writes_marker(tmp_path):
    dep = make_mock_dep(tmp_path)
    fake_deps = ['some_dep_source_placeholder']

    def fake_fetch(probe_target):
        probe_target.dep.from_artifactory = True  # mimic artifactory_load_target side effect
        probe_target.exported_includes = ['/fake/include']
        return (True, fake_deps)

    with patch.object(Git, 'init_commit_hash', return_value='abc1234'), \
         patch.object(artifactory_mod, 'artifactory_fetch_and_reconfigure', side_effect=fake_fetch):
        target, deps = try_load_artifactory_shim(dep)
    assert target is not None
    assert deps is fake_deps
    assert target.exported_includes == ['/fake/include']
    marker = dep.read_shim_marker()
    assert marker['hash'] == 'abc1234'
    assert marker['url'] == 'https://example.com/libfoo.git'
    assert dep.is_artifactory_shim()


def test_uses_resolved_hash_not_tag(tmp_path):
    # Phase 1 contract: init_commit_hash with use_cache=True + fetch_remote=True.
    # Tag-vs-hash logic lives in init_commit_hash; the probe just trusts what it gets.
    dep = make_mock_dep(tmp_path)
    with patch.object(Git, 'init_commit_hash', return_value='def5678') as hash_mock, \
         patch.object(artifactory_mod, 'artifactory_fetch_and_reconfigure',
                      side_effect=lambda pt: (setattr(pt.dep, 'from_artifactory', True), (True, []))[1]):
        target, _ = try_load_artifactory_shim(dep)
    assert target is not None
    assert hash_mock.call_args.kwargs == {'use_cache': True, 'fetch_remote': True}
    assert dep.read_shim_marker()['hash'] == 'def5678'


def test_skipped_for_non_git_dep(tmp_path):
    dep = make_mock_dep(tmp_path)
    dep.dep_source.is_git = False
    with patch.object(Git, 'init_commit_hash') as hash_mock, \
         patch.object(artifactory_mod, 'artifactory_fetch_and_reconfigure') as fetch_mock:
        target, deps = try_load_artifactory_shim(dep)
    assert target is None and deps is None
    hash_mock.assert_not_called()
    fetch_mock.assert_not_called()
