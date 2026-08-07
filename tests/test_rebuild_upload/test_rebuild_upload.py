"""Pins `mama rebuild upload <target>`: it ignores every artifactory product of that target, builds
from source, and uploads what it just built."""
from unittest.mock import Mock, patch

import pytest

from testutils import deploy_pass_uploads, make_exporting_target, make_mock_dep, make_mock_shim_dep

import mama.build_dependency as build_dependency
from mama.build_dependency import BuildDependency


def _rebuild_upload(tmp_path, shim: bool, **over):
    """A dep of `mama rebuild upload libfoo`, with or without a cached shim of an earlier run."""
    make = make_mock_shim_dep if shim else make_mock_dep
    dep = make(tmp_path, rebuild=True, clean=True, build=True, upload=True, target='libfoo', **over)
    dep.config.target_matches.side_effect = lambda name: name == 'libfoo'
    dep.config.targets_all.return_value = False
    dep.config.no_target.return_value = False
    dep.config.deploy = False
    return dep


def test_the_shim_of_the_target_goes_and_nothing_reloads_it(tmp_path):
    dep = _rebuild_upload(tmp_path, shim=True)
    assert dep.is_artifactory_shim()
    with patch.object(BuildDependency, 'try_load_cached_shim', side_effect=AssertionError('shim loaded')), \
         patch.object(build_dependency, 'try_load_artifactory_shim', side_effect=AssertionError('probe ran')):
        assert dep._try_artifactory_shim() is False
    assert not dep.is_artifactory_shim()
    # the same flag the post-clone probe reads, so no later pass can pull the package over the clone
    assert dep.did_check_artifactory


@pytest.mark.parametrize('shim', [True, False])
def test_no_pass_of_the_run_may_fetch_the_target(tmp_path, shim):
    # A local target never reaches the pre-clone shim path, so this gate is the only one it has.
    dep = _rebuild_upload(tmp_path, shim=shim)
    dep.did_check_artifactory = False
    assert dep.can_fetch_artifactory(print=False, which='LOAD') is False


def test_the_cleaned_target_always_builds_once_its_marker_is_gone(tmp_path):
    # Order matters: a shim has no source, so the build decision refuses until the load drops the marker.
    dep = _rebuild_upload(tmp_path, shim=True)
    target = Mock(args=[], build_products=[])
    target.name = 'libfoo'
    decide = lambda: dep._should_build(dep.config, target, True, git_changed=False, loaded_from_pkg=False)
    assert decide() is False
    dep._try_artifactory_shim()
    assert decide() is True


def _uploads(dep) -> bool:
    """True when the deploy pass of this dep reaches papa_upload_to."""
    return deploy_pass_uploads(make_exporting_target(dep, includes=[], libs=[]))


def test_a_shim_still_refuses_to_upload(tmp_path):
    # The guard that the rebuild has to lift: a shim is read-only and its package is already on the server.
    assert not _uploads(_rebuild_upload(tmp_path, shim=True))


def test_the_rebuilt_target_uploads(tmp_path):
    dep = _rebuild_upload(tmp_path, shim=True)
    dep._try_artifactory_shim()   # the rebuild drops the marker
    assert _uploads(dep)
