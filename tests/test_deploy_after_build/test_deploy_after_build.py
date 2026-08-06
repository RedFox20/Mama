"""Pins `deploy_after_build` and the one-line deploy summary of a build."""
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from testutils import make_configured_target

from mama.build_config import DeployStats
import mama.dependency_chain as dc


def _target(tmp_path, deploy_after_build=False, current=True, **over):
    target, dep = make_configured_target(tmp_path, **over)
    dep.should_rebuild = True
    dep.nothing_to_build = False
    dep.from_artifactory = False
    target.deploy_after_build = deploy_after_build
    target.is_current_target = lambda: current
    target.config.no_specific_target.return_value = False
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


# --- nothing_to_upload ------------------------------------------------------

def _uploaded(target) -> bool:
    with patch('mama.papa_upload.papa_upload_to') as upload, patch.object(type(target), 'deploy'):
        target._execute_deploy_tasks()
    return upload.called


def test_an_upload_skips_a_target_that_declares_nothing_to_upload(tmp_path):
    # an application at the root builds no package, so `mama upload` must not demand a papa.txt of it
    target = _target(tmp_path, deploy=False, upload=True)
    target.nothing_to_upload()
    assert not _uploaded(target)


def test_a_target_that_declares_nothing_still_uploads_by_default(tmp_path):
    assert _uploaded(_target(tmp_path, deploy=False, upload=True))


# --- the one-line summary ---------------------------------------------------

def _summary(stats) -> str:
    deps = [SimpleNamespace(config=SimpleNamespace(print=False, debug=False, deploy_stats=stats),
                            build_dir='', should_rebuild=False, from_artifactory=False, nothing_to_build=True)]
    with patch('mama.dependency_chain.console') as console:
        dc._print_build_summary(deps, 1.0)
    return ' | '.join(str(c[0][0]) for c in console.call_args_list)


def test_a_run_that_deployed_nothing_says_nothing(tmp_path):
    assert 'Deployed' not in _summary(DeployStats())


def test_the_summary_names_the_one_dir_it_deployed_to():
    stats = DeployStats()
    with stats.recording():
        stats.record('/pkg/foo/deploy', (2, 3, 0, 0))
    assert 'Deployed 2 includes, 3 libs to /pkg/foo/deploy' in _summary(stats)


def test_the_summary_counts_every_package_the_hook_delegated_to():
    # The root deploys three dep packages of its own, and the user asked one question: what did it write?
    stats = DeployStats()
    with stats.recording():
        for i in range(3): stats.record(f'/pkg/dep{i}/deploy', (1, 2, 1, 0))
    assert 'Deployed 3 includes, 6 libs, 3 syslibs to 3 package dirs' in _summary(stats)


def test_a_deploy_outside_the_window_of_the_current_target_stays_out():
    # 30 deps deploying their own packages must not answer for the target the run named.
    stats = DeployStats()
    stats.record('/pkg/other/deploy', (99, 99, 0, 0))
    with stats.recording(enabled=False):
        stats.record('/pkg/another/deploy', (99, 99, 0, 0))
    assert stats.summary_line() == ''


@pytest.mark.parametrize('is_root, named, current, records', [
    (True,  False, False, True),    # `mama build`: the root is the target the user watches
    (False, False, False, False),   # ...and its 30 deps are not
    (False, True,  True,  True),    # `mama build <target>`: only that target
    (False, True,  False, False),
])
def test_only_one_target_of_the_run_reports_its_deploys(tmp_path, is_root, named, current, records):
    target = _target(tmp_path, current=current)
    target.dep.is_root = is_root
    target.config.no_specific_target.return_value = not named
    stats = target.config.deploy_stats
    with target._recording_deploys():
        stats.record('/pkg/foo/deploy', (1, 1, 0, 0))
    assert bool(stats.summary_line()) is records


def test_a_build_hook_that_deploys_still_counts(tmp_path):
    # The mamafile pattern this replaces: build() ends with self.deploy(), so the window has to span
    # the build hook as well, and it has to survive the deploy window nested inside it.
    target = _target(tmp_path)
    stats = target.config.deploy_stats
    def build_then_deploy():
        with stats.recording():   # the nested window of the deploy pass
            stats.record('/pkg/dep/deploy', (1, 2, 0, 0))
        stats.record('/pkg/root/deploy', (1, 1, 0, 0))
    with patch.object(type(target), 'build', side_effect=build_then_deploy), \
         patch.object(type(target), '_has_custom_build', return_value=True), \
         patch.object(type(target), '_run_configure_once'), \
         patch.object(type(target), '_run_packaging'):
        target.build_phase()
    assert 'Deployed 2 includes, 3 libs to 2 package dirs' in stats.summary_line()
