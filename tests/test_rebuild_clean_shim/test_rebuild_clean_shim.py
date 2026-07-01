"""Pins rebuild/clean of a cached artifactory shim: `rebuild` drops the shim and builds from source;
a plain `clean` re-extracts the package after wiping the build dir (so dependents can still link)."""
from unittest.mock import Mock, patch

from testutils import make_mock_shim_dep

import mama.build_dependency as build_dependency
from mama.build_dependency import BuildDependency
from mama.types.git import Git


def test_rebuild_forces_source_clone_like_unshallow(tmp_path):
    dep = make_mock_shim_dep(tmp_path, rebuild=True)
    dep.config.target_matches.return_value = True
    assert dep._force_source_clone()
    dep.config.target_matches.return_value = False
    assert not dep._force_source_clone()   # only the rebuilt target, not every dep in a targeted run


def test_rebuild_target_drops_cached_shim_for_source(tmp_path):
    dep = make_mock_shim_dep(tmp_path, rebuild=True)
    dep.config.target_matches.return_value = True
    assert dep.is_artifactory_shim()
    assert dep._try_artifactory_shim() is False   # cached shim NOT loaded
    assert not dep.is_artifactory_shim()           # marker dropped -> _git_checkout_if_needed clones source


def test_plain_clean_reloads_pkg_without_a_source_clone(tmp_path):
    dep = make_mock_shim_dep(tmp_path, clean=True)
    dep.config.target_matches.return_value = True
    assert not dep._force_source_clone()   # a plain clean must not force a source clone
    with patch.object(BuildDependency, 'try_load_cached_shim', return_value=Mock()), \
         patch.object(Git, 'dependency_checkout') as clone_mock, \
         patch.object(build_dependency, 'artifactory_fetch_and_reconfigure',
                      return_value=(True, [])) as reload_mock:
        dep._load()
    reload_mock.assert_called_once()   # clean rmtree'd the pkg libs -> re-extract from the cached zip
    clone_mock.assert_not_called()     # ...and never fall back to cloning source
