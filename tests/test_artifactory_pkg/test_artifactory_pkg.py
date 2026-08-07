"""Pins that an `add_artifactory_pkg` dep is mandatory and read-only: no flag refuses its fetch, and
it never deploys or uploads."""
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from testutils import make_configured_target, make_mock_dep

from mama.build_target import BuildTarget


def _as_pkg(dep):
    """Turn a mock dep into an artifactory pkg dep. Only `is_pkg` decides the rules under test."""
    dep.dep_source = SimpleNamespace(is_pkg=True, is_git=False, is_src=False)
    dep.did_check_artifactory = False
    return dep


@pytest.mark.parametrize('flag', ['disable_artifactory', 'rebuild', 'clean'])
def test_no_flag_refuses_the_fetch_of_a_pkg_dep(tmp_path, flag):
    # a pkg dep has no source, so a refusal leaves it with nothing to export
    dep = make_mock_dep(tmp_path, **{flag: True})
    dep.config.target_matches.return_value = True  # rebuild and clean only refuse the named target
    assert dep.can_fetch_artifactory(print=False, which='LOAD') is False
    assert _as_pkg(dep).can_fetch_artifactory(print=False, which='LOAD') is True


def test_noart_still_refuses_a_git_dep(tmp_path):
    dep = make_mock_dep(tmp_path, disable_artifactory=True)
    assert dep.can_fetch_artifactory(print=False, which='LOAD') is False


def _deploy_run(tmp_path, is_pkg):
    target, dep = make_configured_target(tmp_path, deploy=False, upload=True)
    dep.dep_source = SimpleNamespace(is_pkg=is_pkg, is_git=False, is_src=True)
    target.is_current_target = lambda: True
    with patch('mama.papa_upload.papa_upload_to') as upload, patch.object(BuildTarget, 'deploy') as deploy:
        target._execute_deploy_tasks()
    return deploy.called, upload.called


def test_a_pkg_dep_never_deploys_or_uploads(tmp_path):
    # the artifactory already holds it, and this run built nothing to publish over it
    assert _deploy_run(tmp_path, is_pkg=True) == (False, False)


def test_a_source_dep_still_deploys_and_uploads(tmp_path):
    assert _deploy_run(tmp_path, is_pkg=False) == (True, True)
