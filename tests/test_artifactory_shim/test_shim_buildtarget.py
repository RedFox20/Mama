"""BuildTarget-level shim behaviour: _require_source + _execute_deploy_tasks."""
from unittest.mock import patch

import pytest
from testutils import make_mock_dep

from mama.build_target import BuildTarget


def _make_target(tmp_path, as_shim: bool, **cfg):
    # current-target so _execute_deploy_tasks runs at all
    cfg.setdefault('deploy', True)
    cfg.setdefault('upload', False)
    dep = make_mock_dep(tmp_path, target_matches=lambda _: True, **cfg)
    dep.config.no_target = lambda: False
    dep.config.targets_all = lambda: False
    if as_shim:
        dep.write_shim_marker(archive_name='libfoo-linux-22-gcc11.3-x64-release-abc1234',
                              commit_hash='abc1234')
    return dep, BuildTarget(name='libfoo', config=dep.config, dep=dep, args=[])


def test_require_source_refuses_on_shim(tmp_path):
    _, target = _make_target(tmp_path, as_shim=True)
    assert target._require_source('test') is False


def test_require_source_allows_on_non_shim(tmp_path):
    _, target = _make_target(tmp_path, as_shim=False)
    assert target._require_source('test') is True


def test_execute_deploy_tasks_skips_deploy_for_shim(tmp_path):
    _, target = _make_target(tmp_path, as_shim=True)
    deploy_ran = []
    target.deploy = lambda: deploy_ran.append(True)
    with patch('mama.build_target.papa_upload_to') as upload_mock:
        target._execute_deploy_tasks()
        upload_mock.assert_not_called()
    assert not deploy_ran


def test_execute_deploy_tasks_runs_deploy_for_non_shim(tmp_path):
    _, target = _make_target(tmp_path, as_shim=False)
    deploy_ran = []
    target.deploy = lambda: deploy_ran.append(True)
    target._execute_deploy_tasks()
    assert deploy_ran


@pytest.mark.parametrize('if_needed', [False, True])
def test_execute_deploy_tasks_skips_upload_for_shim(tmp_path, if_needed):
    # shim => already on artifactory; upload is a no-op success (returns, no raise) regardless of if_needed
    _, target = _make_target(tmp_path, as_shim=True, deploy=False, upload=True, if_needed=if_needed)
    with patch('mama.build_target.papa_upload_to') as upload_mock:
        target._execute_deploy_tasks()
        upload_mock.assert_not_called()


def test_execute_deploy_tasks_uploads_for_non_shim(tmp_path):
    _, target = _make_target(tmp_path, as_shim=False, deploy=False, upload=True)
    target.deploy = lambda: None
    with patch('mama.build_target.papa_upload_to') as upload_mock:
        target._execute_deploy_tasks()
        upload_mock.assert_called_once()
