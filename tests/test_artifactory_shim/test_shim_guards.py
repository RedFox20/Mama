"""Shim-aware guards across BuildDependency / BuildTarget / papa_deploy."""
import os
from unittest.mock import Mock, patch

import pytest

from testutils import make_mock_dep

from mama.build_dependency import BuildDependency
from mama.types.git import Git
from mama.papa_deploy import papa_deploy_to


def _make_shim(tmp_path):
    dep = make_mock_dep(tmp_path, build=True)
    dep.write_shim_marker(archive_name='libfoo-linux-22-gcc11.3-x64-release-abc1234',
                          commit_hash='abc1234')
    return dep


def test_update_mamafile_tag_returns_false_for_shim(tmp_path):
    dep = _make_shim(tmp_path)
    assert dep.is_artifactory_shim()
    assert dep.update_mamafile_tag() is False
    assert dep.update_cmakelists_tag() is False


def test_should_build_returns_false_for_shim_even_with_update_target(tmp_path):
    dep = _make_shim(tmp_path)
    dep.config.update = True
    dep.config.target = 'libfoo'
    target = Mock(name='libfoo', args=[], build_products=[])
    target.name = 'libfoo'
    assert dep._should_build(dep.config, target, is_target=True,
                             git_changed=False, loaded_from_pkg=True) is False


def test_should_build_returns_false_for_shim_with_clean_target(tmp_path):
    dep = _make_shim(tmp_path)
    dep.config.clean = True
    dep.config.target = 'libfoo'
    target = Mock(args=[])
    target.name = 'libfoo'
    assert dep._should_build(dep.config, target, is_target=True,
                             git_changed=False, loaded_from_pkg=True) is False


def test_dirty_removes_shim_marker(tmp_path):
    dep = _make_shim(tmp_path)
    dep.target = Mock(build_products=[])
    assert os.path.exists(dep.mama_shim_file())
    dep.dirty()
    assert not os.path.exists(dep.mama_shim_file())
    assert not dep.is_artifactory_shim()


def test_papa_deploy_to_refuses_with_shim_marker_in_destination(tmp_path):
    # If deployed into the shim's build_dir, we'd corrupt the artifactory snapshot.
    dep = _make_shim(tmp_path)
    target = Mock()
    target.config.print = False
    target.config.verbose = False
    target.config.test = False
    target.is_current_target.return_value = False
    target.name = 'libfoo'
    with pytest.raises(RuntimeError, match='mama_shim marker'):
        papa_deploy_to(target, dep.build_dir,
                       r_includes=False, r_dylibs=False, r_syslibs=False, r_assets=False)


def test_git_checkout_if_needed_short_circuits_for_shim(tmp_path):
    # Without this guard, a shim with a missing src_dir falls through to
    # dependency_checkout, which walks up the parent dir and queries the wrong remote.
    dep = _make_shim(tmp_path)
    called = []
    with patch.object(Git, 'dependency_checkout', side_effect=lambda d: called.append(d) or True):
        result = dep._git_checkout_if_needed()
    assert result is False
    assert called == []


def test_run_git_raises_on_shim(tmp_path):
    dep = _make_shim(tmp_path)
    with pytest.raises(RuntimeError, match='artifactory shim'):
        dep.dep_source.run_git(dep, 'fetch origin main -q')


def test_run_git_returns_nonzero_when_not_throwing_on_shim(tmp_path):
    # _has_local_modifications calls run_git(throw=False); must see a non-zero rc, not silent success.
    dep = _make_shim(tmp_path)
    assert dep.dep_source.run_git(dep, 'diff --quiet HEAD', throw=False) != 0


def test_is_artifactory_shim_caches_filesystem_stat(tmp_path):
    # Called per-progress-tick and per-git-op; must not stat on every call.
    dep = _make_shim(tmp_path)
    assert dep.is_artifactory_shim() is True
    with patch('os.path.exists', side_effect=AssertionError('stat called')):
        for _ in range(10):
            assert dep.is_artifactory_shim() is True


def test_is_artifactory_shim_cache_updates_on_remove(tmp_path):
    dep = _make_shim(tmp_path)
    assert dep.is_artifactory_shim() is True
    dep.remove_shim_marker()
    with patch('os.path.exists', side_effect=AssertionError('stat called')):
        assert dep.is_artifactory_shim() is False


def test_is_artifactory_shim_cache_invalidated_on_write(tmp_path):
    # Invalidate (not preset True) so a coexisting .git wins.
    dep = make_mock_dep(tmp_path)
    assert dep.is_artifactory_shim() is False
    dep.write_shim_marker(archive_name='archive', commit_hash='abc1234')
    assert dep.is_artifactory_shim() is True


def test_papa_deploy_to_succeeds_for_normal_destination(tmp_path):
    deploy_dir = tmp_path / 'deploy' / 'libfoo'
    deploy_dir.mkdir(parents=True)
    target = Mock()
    target.config.print = False
    target.config.verbose = False
    target.config.test = False
    target.is_current_target.return_value = False
    target.name = 'libfoo'
    target.exported_includes = []
    target.exported_libs = []
    target.exported_syslibs = []
    target.exported_assets = []
    target.includes_root = ('', '', '')
    target.children.return_value = []
    target.build_dir.return_value = str(deploy_dir)
    target.source_dir.return_value = str(deploy_dir)
    papa_deploy_to(target, str(deploy_dir),
                   r_includes=False, r_dylibs=False, r_syslibs=False, r_assets=False)
    assert (deploy_dir / 'papa.txt').exists()
