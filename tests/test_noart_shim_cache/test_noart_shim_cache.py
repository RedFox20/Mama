"""noart must honour an existing shim cache (no fetch, but ls-remote staleness check)."""
from unittest.mock import Mock, patch

from testutils import make_mock_dep, make_mock_shim_dep

from mama.build_dependency import BuildDependency
from mama.types.git import Git


class TestNoartShimCacheHit:
    def test_returns_target_when_hash_matches(self, tmp_path):
        dep = make_mock_shim_dep(tmp_path, write_papa_txt=True, disable_artifactory=True)
        with patch.object(Git, 'init_commit_hash', return_value='abc1234'), \
             patch('mama.artifactory.artifactory_load_target', return_value=(True, [])) as mock_load:
            target = dep.try_load_cached_shim()
        assert target is not None and target.name == 'libfoo'
        assert mock_load.call_args.args[1] == dep.build_dir  # must load from local build_dir, not artifactory
        assert dep.is_artifactory_shim()

    def test_shim_dependencies_are_added_as_children(self, tmp_path):
        dep = make_mock_shim_dep(tmp_path, write_papa_txt=True, disable_artifactory=True)
        child_dep_source = Mock(name='child')
        with patch.object(Git, 'init_commit_hash', return_value='abc1234'), \
             patch('mama.artifactory.artifactory_load_target', return_value=(True, [child_dep_source])), \
             patch.object(BuildDependency, 'add_child') as mock_add_child:
            dep.try_load_cached_shim()
        mock_add_child.assert_called_once_with(child_dep_source)


class TestNoartShimCacheStale:
    def test_stale_marker_is_removed(self, tmp_path):
        dep = make_mock_shim_dep(tmp_path, write_papa_txt=True, disable_artifactory=True, stored_hash='abc1234')
        with patch.object(Git, 'init_commit_hash', return_value='def5678'), \
             patch('mama.artifactory.artifactory_load_target') as mock_load:
            target = dep.try_load_cached_shim()
        assert target is None
        assert not dep.is_artifactory_shim()
        mock_load.assert_not_called()  # stale cache must not be loaded


class TestNoartShimCacheMisses:
    def test_no_marker_returns_none(self, tmp_path):
        dep = make_mock_dep(tmp_path, disable_artifactory=True)
        assert dep.try_load_cached_shim() is None

    def test_marker_without_hash_returns_none(self, tmp_path):
        dep = make_mock_dep(tmp_path, disable_artifactory=True)
        dep.mama_shim_file()  # ensure dir exists
        with open(dep.mama_shim_file(), 'w') as f: f.write('shim 1\nname libfoo\n')
        dep._is_shim_cache = None  # marker was just written behind the cache's back
        assert dep.try_load_cached_shim() is None

    def test_ls_remote_failure_does_not_drop_marker(self, tmp_path):
        # Transient network failure should not penalize the dep with a forced re-clone next run.
        dep = make_mock_shim_dep(tmp_path, write_papa_txt=True, disable_artifactory=True)
        with patch.object(Git, 'init_commit_hash', return_value=None), \
             patch('mama.artifactory.artifactory_load_target', return_value=(True, [])):
            target = dep.try_load_cached_shim()
        assert target is not None
        assert dep.is_artifactory_shim()

    def test_corrupted_papa_returns_none(self, tmp_path):
        dep = make_mock_shim_dep(tmp_path, write_papa_txt=True, disable_artifactory=True)
        with patch.object(Git, 'init_commit_hash', return_value='abc1234'), \
             patch('mama.artifactory.artifactory_load_target', return_value=(False, None)):
            assert dep.try_load_cached_shim() is None


class TestNoartRouting:
    def test_noart_routes_to_cached_shim_path(self, tmp_path):
        dep = make_mock_shim_dep(tmp_path, write_papa_txt=True, disable_artifactory=True)
        fake_target = Mock(args=[], settings=Mock(), dependencies=Mock(), build_products=[])
        with patch.object(BuildDependency, 'try_load_cached_shim', return_value=fake_target) as mock_cached, \
             patch('mama.build_dependency.try_load_artifactory_shim') as mock_probe, \
             patch.object(BuildDependency, '_load_target', return_value=fake_target), \
             patch.object(BuildDependency, '_should_build', return_value=False), \
             patch.object(BuildDependency, 'should_load_artifactory', return_value=False), \
             patch.object(BuildDependency, 'load_build_products'):
            dep._load()
        mock_cached.assert_called_once()
        mock_probe.assert_not_called()
