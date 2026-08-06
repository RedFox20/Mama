"""Pins that `mama update` moves a shim forward when upstream advanced and no new package exists."""
from unittest.mock import patch
import pytest

from testutils import make_mock_shim_dep

from mama.build_dependency import BuildDependency
from mama.types.git import Git


def _shim(tmp_path, stored='abc1234', **cfg):
    dep = make_mock_shim_dep(tmp_path, write_papa_txt=True, stored_hash=stored, **cfg)
    return dep


def _probe_misses(dep, resolved):
    """Run _try_artifactory_shim with the package probe missing and the remote at `resolved`."""
    def probe(d):
        d.dep_source.commit_hash = resolved   # try_load_artifactory_shim resolves this before it probes
        return (None, None)
    with patch('mama.build_dependency.try_load_artifactory_shim', side_effect=probe), \
         patch.object(BuildDependency, 'can_fetch_artifactory', return_value=True):
        return dep._try_artifactory_shim()


def test_update_drops_a_shim_whose_commit_upstream_left_behind(tmp_path):
    # without this the dep keeps the package of the old commit, and update moves nothing
    dep = _shim(tmp_path, update=True, print=True)
    assert _probe_misses(dep, 'def5678') is False
    assert not dep.is_artifactory_shim()   # dropped, so _git_checkout_if_needed now clones


def test_update_keeps_a_shim_whose_commit_did_not_move(tmp_path):
    # the package vanished from the server, but the extracted files still match this commit
    dep = _shim(tmp_path, update=True, print=True)
    assert _probe_misses(dep, 'abc1234') is False
    assert dep.is_artifactory_shim()


def test_update_keeps_a_shim_when_the_remote_does_not_answer(tmp_path):
    # a transient network failure must not force a re-clone of every shim
    dep = _shim(tmp_path, update=True, print=True)
    assert _probe_misses(dep, '') is False
    assert dep.is_artifactory_shim()


def test_a_plain_build_never_drops_the_shim(tmp_path):
    # build trusts the cache, and it takes the cached path before the probe can run
    dep = _shim(tmp_path, print=True)
    with patch('mama.artifactory.artifactory_load_target', return_value=(True, [])), \
         patch.object(Git, 'init_commit_hash', side_effect=AssertionError('ls-remote called')):
        assert dep._try_artifactory_shim() is True
    assert dep.is_artifactory_shim()
