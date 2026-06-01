"""Pin: 404 from artifactory for a git dep must NOT wipe git_status (caused spurious SCM-change next run)."""
import os
from unittest.mock import Mock, patch
from urllib.error import HTTPError

import pytest

from mama import artifactory as art
from mama.types.git import Git


def _make_git_target(tmp_path):
    git = Git(name='libfoo', url='https://example.com/libfoo.git',
              branch='main', tag='', mamafile=None, shallow=True, args=[])
    config = Mock(is_network_available=Mock(return_value=True), verbose=False, force_artifactory=False)
    dep = Mock(build_dir=str(tmp_path), dep_source=git, config=config)
    dep.name = 'libfoo'
    target = Mock(config=config, dep=dep)
    target.name = 'libfoo'
    # Seed git_status as a successful prior run would have.
    status_path = git.git_status_file(dep)
    os.makedirs(os.path.dirname(status_path), exist_ok=True)
    with open(status_path, 'w') as f:
        f.write(git.format_git_status(git.url, git.tag, git.branch, 'abc1234'))
    return target, status_path


def _http_404():
    return HTTPError(url='http://example.com/x.zip', code=404, msg='Not Found', hdrs=None, fp=None)


def test_404_does_not_wipe_git_status(tmp_path):
    target, status_path = _make_git_target(tmp_path)
    with patch('mama.artifactory.download_file', side_effect=_http_404()):
        assert art._fetch_package(target, 'example.com', 'libfoo-abc1234', str(tmp_path)) is None
    # 404 means "no archive for this commit", not "git source is stale".
    assert os.path.exists(status_path)


def test_404_on_is_pkg_still_raises(tmp_path):
    # is_pkg URLs are mandatory - a 404 there IS fatal.
    from mama.types.artifactory_pkg import ArtifactoryPkg
    pkg = ArtifactoryPkg(name='libfoo', version='1.0', fullname='libfoo-1.0')
    config = Mock(is_network_available=Mock(return_value=True), verbose=False, force_artifactory=False)
    dep = Mock(build_dir=str(tmp_path), dep_source=pkg, config=config)
    dep.name = 'libfoo'
    target = Mock(config=config, dep=dep)
    target.name = 'libfoo'
    with patch('mama.artifactory.download_file', side_effect=_http_404()), \
         pytest.raises(RuntimeError, match='did not exist'):
        art._fetch_package(target, 'example.com', 'libfoo-1.0', str(tmp_path))


def test_non_404_network_error_does_not_wipe_git_status_either(tmp_path):
    target, status_path = _make_git_target(tmp_path)
    with patch('mama.artifactory.is_network_error', return_value=True), \
         patch('mama.artifactory.download_file', side_effect=ConnectionRefusedError()):
        assert art._fetch_package(target, 'example.com', 'libfoo-abc1234', str(tmp_path)) is None
    assert os.path.exists(status_path)
