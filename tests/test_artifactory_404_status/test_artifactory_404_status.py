"""Pin: 404 from artifactory for a git dep must NOT wipe git_status (caused spurious SCM-change next run)."""
import os
from unittest.mock import Mock, patch

import pytest

from mama import artifactory as art
from mama.types.git import Git
from mama.utils.net import DOWNLOAD_TIMEOUT, DownloadError


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


def _failed_download(reason='HTTP 404 Not Found', status=404, network=False):
    """A download reports a failure as (None, DownloadError), so it raises nothing at the caller."""
    return (None, DownloadError('http://example.com/x.zip', reason, status=status, network=network))


def test_404_does_not_wipe_git_status(tmp_path):
    target, status_path = _make_git_target(tmp_path)
    with patch('mama.artifactory.try_download_file', return_value=_failed_download()):
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
    with patch('mama.artifactory.try_download_file', return_value=_failed_download()), \
         pytest.raises(RuntimeError, match='did not exist'):
        art._fetch_package(target, 'example.com', 'libfoo-1.0', str(tmp_path))


def test_non_404_network_error_does_not_wipe_git_status_either(tmp_path):
    target, status_path = _make_git_target(tmp_path)
    refused = _failed_download('ConnectionRefusedError: refused', status=0, network=True)
    with patch('mama.artifactory.try_download_file', return_value=refused):
        assert art._fetch_package(target, 'example.com', 'libfoo-abc1234', str(tmp_path)) is None
    assert os.path.exists(status_path)
    target.config.mark_network_unavailable.assert_called_once()  # a dead network stops the next fetch


def test_a_404_leaves_the_network_marked_available(tmp_path):
    target, _ = _make_git_target(tmp_path)
    with patch('mama.artifactory.try_download_file', return_value=_failed_download()):
        art._fetch_package(target, 'example.com', 'libfoo-abc1234', str(tmp_path))
    target.config.mark_network_unavailable.assert_not_called()


def test_the_fetch_uses_the_short_timeout(tmp_path):
    # a cached package or a source build answers the same question, so a dead network must not cost 30s
    target, _ = _make_git_target(tmp_path)
    with patch('urllib.request.urlopen', side_effect=TimeoutError('timed out')) as opened:
        assert art._fetch_package(target, 'example.com', 'libfoo-abc1234', str(tmp_path)) is None
    assert opened.call_args.kwargs['timeout'] == DOWNLOAD_TIMEOUT
