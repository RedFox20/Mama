"""Pins that a package whose `D` records pin a git dep other than the root lock is a cache miss."""
import os
from unittest.mock import patch

from testutils import make_mock_dep

import mama.artifactory as artifactory_mod
from mama.build_target import BuildTarget
from mama.dependency_lock import DependencyLock, LockEntry, LockSelector
from mama.utils.fileio import write_text_to

KRATTLINK = 'git krattlink,https://example.com/org/krattlink.git,{branch},,'


def _locked_package(tmp_path, branch: str):
    """A package dep under a root lock that pins krattlink to develop, and whose papa.txt names `branch`."""
    entry = LockEntry('krattlink', 'example.com/org/krattlink', LockSelector('branch', 'develop'), 'a' * 40)
    dep = make_mock_dep(tmp_path, name='service', dependency_lock=DependencyLock('', ('linux',), {'krattlink': entry}))
    write_text_to(dep.papa_package_file(), f'P service\nD {KRATTLINK.format(branch=branch)}\n')
    dep.write_shim_marker(archive_name='service-x', commit_hash='abc1234')
    return BuildTarget(name='service', config=dep.config, dep=dep, args=[])


def test_a_package_with_another_pin_loads_from_source(tmp_path):
    target = _locked_package(tmp_path, 'feature/krattnode')
    with patch.object(artifactory_mod, 'warning', autospec=True) as warn:
        assert artifactory_mod.artifactory_load_target(target, target.dep.build_dir, 0) == (False, None)
    assert 'branch=feature/krattnode' in warn.call_args.args[0] and 'branch=develop' in warn.call_args.args[0]
    dep = target.dep
    assert dep.stale_package_cause and dep.did_check_artifactory and not dep.from_artifactory
    assert not dep.is_artifactory_shim() and not os.path.exists(dep.papa_package_file())
    assert not dep.config.dependency_lock._declarations  # the reject records nothing


def test_a_package_with_the_locked_pin_loads(tmp_path):
    target = _locked_package(tmp_path, 'develop')
    fetched, children = artifactory_mod.artifactory_load_target(target, target.dep.build_dir, 0)
    assert fetched and [c.name for c in children] == ['krattlink']
    assert not target.dep.stale_package_cause


def test_a_rejected_cached_package_is_not_downloaded_again(tmp_path):
    target = _locked_package(tmp_path, 'feature/krattnode')
    write_text_to(f'{target.dep.dep_dir}/service-x.zip', 'zip')
    target.version = '1.0.0'  # a version skips the mamafile read
    with patch.object(artifactory_mod, 'artifactory_archive_name', autospec=True, return_value='service-x'), \
         patch.object(artifactory_mod, 'try_unzip', autospec=True, return_value=(True, 0)), \
         patch.object(artifactory_mod, '_fetch_package', autospec=True) as fetch:
        assert artifactory_mod.artifactory_fetch_and_reconfigure(target) == (False, None)
    fetch.assert_not_called()


def test_a_cached_shim_with_another_pin_clones(tmp_path):
    dep = _locked_package(tmp_path, 'feature/krattnode').dep
    assert dep.try_load_cached_shim(check_staleness=False) is None
    assert not dep.is_artifactory_shim() and dep.did_check_artifactory and dep.stale_package_cause


def test_a_later_accepted_package_clears_the_reject(tmp_path):
    target = _locked_package(tmp_path, 'feature/krattnode')
    artifactory_mod.artifactory_load_target(target, target.dep.build_dir, 0)
    write_text_to(target.dep.papa_package_file(), f'P service\nD {KRATTLINK.format(branch="develop")}\n')
    assert artifactory_mod.artifactory_load_target(target, target.dep.build_dir, 0)[0]
    assert not target.dep.stale_package_cause
