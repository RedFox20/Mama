"""Pins `deploy_after_build`: a build deploys when the target asks for it, and only then."""
from unittest.mock import patch

import pytest

from testutils import make_configured_target


def _target(tmp_path, deploy_after_build=False, current=True, **over):
    target, dep = make_configured_target(tmp_path, **over)
    dep.should_rebuild = True
    dep.nothing_to_build = False
    dep.from_artifactory = False
    target.deploy_after_build = deploy_after_build
    target.is_current_target = lambda: current
    return target


def _built(target) -> bool:
    """Run the build phase with the compile and the packaging stubbed, and report a deploy."""
    with patch.object(type(target), 'deploy') as deploy, \
         patch.object(type(target), '_cmake_build_step'), \
         patch.object(type(target), '_run_packaging'):
        target.build_phase()
    return deploy.called


def test_a_plain_build_never_deploys(tmp_path):
    assert not _built(_target(tmp_path))


def test_deploy_after_build_deploys(tmp_path):
    assert _built(_target(tmp_path, deploy_after_build=True))


@pytest.mark.parametrize('state', ['nothing_to_build', 'from_artifactory'])
def test_a_target_with_no_build_work_never_deploys(tmp_path, state):
    # An up-to-date or fetched target has nothing new to place beside the binaries.
    target = _target(tmp_path, deploy_after_build=True)
    setattr(target.dep, state, True)
    assert not _built(target)


def test_the_deploy_hook_runs_once_for_a_build_and_an_upload(tmp_path):
    target = _target(tmp_path, deploy_after_build=True, deploy=False, upload=True)
    target.dep.is_root = False
    with patch.object(type(target), 'deploy') as deploy, \
         patch.object(type(target), '_cmake_build_step'), \
         patch.object(type(target), '_run_packaging'), \
         patch('mama.papa_upload.papa_upload_to'):
        target.build_phase()
        target._execute_deploy_tasks()
    deploy.assert_called_once()
