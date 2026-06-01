"""Plain `mama build` must trust an existing shim - no ls-remote, no re-unzip."""
from unittest.mock import patch

from testutils import make_mock_dep, make_mock_shim_dep

from mama.build_dependency import BuildDependency
from mama.types.git import Git


class TestPlainBuildHonoursShim:
    def test_cached_path_taken_without_ls_remote(self, tmp_path):
        dep = make_mock_shim_dep(tmp_path, write_papa_txt=True)
        with patch.object(Git, 'init_commit_hash', side_effect=AssertionError('ls-remote called')), \
             patch('mama.build_dependency.try_load_artifactory_shim', side_effect=AssertionError('probe called')) as mock_probe, \
             patch('mama.artifactory.artifactory_load_target', return_value=(True, [])) as mock_load:
            took_cached = dep._try_artifactory_shim()
        assert took_cached is True
        assert dep.did_check_artifactory is True
        mock_probe.assert_not_called()
        assert mock_load.call_args.args[1] == dep.build_dir

    def test_update_skips_cached_path(self, tmp_path):
        dep = make_mock_shim_dep(tmp_path, write_papa_txt=True, update=True)
        with patch.object(BuildDependency, 'try_load_cached_shim') as mock_cached, \
             patch('mama.build_dependency.try_load_artifactory_shim', return_value=(None, None)) as mock_probe, \
             patch.object(BuildDependency, 'can_fetch_artifactory', return_value=True):
            dep._try_artifactory_shim()
        mock_cached.assert_not_called()
        mock_probe.assert_called_once()

    def test_no_shim_falls_through_to_probe(self, tmp_path):
        dep = make_mock_dep(tmp_path)
        with patch('mama.build_dependency.try_load_artifactory_shim', return_value=(None, None)) as mock_probe, \
             patch.object(BuildDependency, 'can_fetch_artifactory', return_value=True):
            took_cached = dep._try_artifactory_shim()
        assert took_cached is False
        mock_probe.assert_called_once()


class TestCachedShimStalenessGate:
    def test_check_staleness_false_skips_ls_remote(self, tmp_path):
        dep = make_mock_shim_dep(tmp_path, write_papa_txt=True)
        with patch.object(Git, 'init_commit_hash', side_effect=AssertionError('ls-remote called')), \
             patch('mama.artifactory.artifactory_load_target', return_value=(True, [])):
            target = dep.try_load_cached_shim(check_staleness=False)
        assert target is not None and target.name == 'libfoo'
        assert dep.is_artifactory_shim()

    def test_check_staleness_true_drops_stale_marker(self, tmp_path):
        dep = make_mock_shim_dep(tmp_path, write_papa_txt=True, stored_hash='abc1234')
        with patch.object(Git, 'init_commit_hash', return_value='def5678'), \
             patch('mama.artifactory.artifactory_load_target') as mock_load:
            target = dep.try_load_cached_shim(check_staleness=True)
        assert target is None
        assert not dep.is_artifactory_shim()
        mock_load.assert_not_called()
