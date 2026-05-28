"""Regression test for the 'SCM change detected on second mama update' bug.

Background: when artifactory returned 404 for a git dep (normal — there's just
no prebuilt archive for the current commit), the previous _fetch_package code
deleted the git_status file via Git.reset_status(). The next ``mama update``
then read an empty status, treated the dep as first-time, and printed
``Pulling X SCM change detected`` followed by a full rebuild — even though
nothing in the source had changed.

This test pins the corrected behaviour: a 404 on a git dep MUST NOT touch
the git_status file. The mamafile-level url/tag/branch/commit comparison in
check_status already handles legitimate source changes; 404 only means
"no archive for this commit on the server", which is normal and benign.
"""
from __future__ import annotations

import os
import sys
import tempfile
import shutil
from unittest.mock import Mock, patch
from urllib.error import HTTPError

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))
from mama import artifactory as art  # noqa: E402
from mama.types.git import Git  # noqa: E402


def _make_target_with_status(tmpdir):
    """Build a BuildTarget-shaped stub whose git_status file already exists."""
    git = Git(name='libfoo', url='https://example.com/libfoo.git',
              branch='main', tag='', mamafile=None, shallow=True, args=[])

    config = Mock()
    config.is_network_available.return_value = True
    config.verbose = False
    config.force_artifactory = False

    dep = Mock()
    dep.name = 'libfoo'
    dep.build_dir = tmpdir
    dep.dep_source = git
    dep.config = config

    target = Mock()
    target.name = 'libfoo'
    target.config = config
    target.dep = dep

    # Pre-populate the git_status file as a successful prior run would have.
    status_path = git.git_status_file(dep)
    os.makedirs(os.path.dirname(status_path), exist_ok=True)
    with open(status_path, 'w') as f:
        f.write(git.format_git_status(git.url, git.tag, git.branch, 'abc1234'))
    return target, status_path


def _http_404():
    """A 404 HTTPError instance matching what urllib.request.urlopen raises."""
    return HTTPError(url='http://example.com/x.zip', code=404,
                     msg='Not Found', hdrs=None, fp=None)


def test_404_does_not_wipe_git_status():
    """The bug: a 404 fetch was deleting git_status, causing the next
    `mama update` to report 'SCM change detected' on an unchanged dep."""
    tmpdir = tempfile.mkdtemp(prefix='mama_404_test_')
    try:
        target, status_path = _make_target_with_status(tmpdir)
        assert os.path.exists(status_path), 'precondition: status file exists'

        with patch('mama.artifactory.download_file', side_effect=_http_404()):
            result = art._fetch_package(target, 'example.com', 'libfoo-abc1234', tmpdir)

        assert result is None, 'fetch must report miss'
        assert os.path.exists(status_path), (
            'git_status was deleted on 404 — this is the regression bug. '
            'A 404 means "no archive for this commit", not "git source is stale".'
        )
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_404_on_is_pkg_still_raises():
    """For an artifactory-only pkg dep (not git), a 404 IS fatal —
    those URLs must exist."""
    from mama.types.artifactory_pkg import ArtifactoryPkg
    tmpdir = tempfile.mkdtemp(prefix='mama_404_test_')
    try:
        pkg = ArtifactoryPkg(name='libfoo', version='1.0', fullname='libfoo-1.0')

        config = Mock()
        config.is_network_available.return_value = True
        config.verbose = False
        config.force_artifactory = False

        dep = Mock()
        dep.name = 'libfoo'
        dep.build_dir = tmpdir
        dep.dep_source = pkg
        dep.config = config

        target = Mock()
        target.name = 'libfoo'
        target.config = config
        target.dep = dep

        with patch('mama.artifactory.download_file', side_effect=_http_404()):
            with pytest.raises(RuntimeError, match='did not exist'):
                art._fetch_package(target, 'example.com', 'libfoo-1.0', tmpdir)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_non_404_network_error_does_not_wipe_git_status_either():
    """Connection refused / timeout should also leave status untouched —
    these are transient and shouldn't trigger a spurious rebuild later."""
    tmpdir = tempfile.mkdtemp(prefix='mama_404_test_')
    try:
        target, status_path = _make_target_with_status(tmpdir)

        with patch('mama.artifactory.is_network_error', return_value=True), \
             patch('mama.artifactory.download_file', side_effect=ConnectionRefusedError()):
            result = art._fetch_package(target, 'example.com', 'libfoo-abc1234', tmpdir)

        assert result is None
        assert os.path.exists(status_path)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
