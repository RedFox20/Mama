"""Pins `mama unshallow <target>`: a cached shim must be dropped so the git path clones source."""
from testutils import make_mock_shim_dep


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
