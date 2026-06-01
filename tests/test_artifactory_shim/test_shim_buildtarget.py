"""BuildTarget-level shim behaviour: _require_source + _execute_deploy_tasks."""
from unittest.mock import patch

from testutils import make_mock_dep

from mama.build_target import BuildTarget


def _make_target(tmp_path, as_shim: bool):
    # current-target so _execute_deploy_tasks runs at all
    dep = make_mock_dep(tmp_path, deploy=True, upload=False,
                        target_matches=lambda _: True)
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
