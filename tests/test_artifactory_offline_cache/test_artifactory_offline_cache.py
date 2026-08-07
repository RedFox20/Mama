"""Pins that a failed download falls back to the cached zip of the same name, instead of ending the run."""
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from testutils import make_package_target

import mama.artifactory as art


def _target(tmp_path, cached: bool, **config):
    """A target whose archive name is fixed, with or without that archive already in its dep dir."""
    target = make_package_target(tmp_path, package=None, **config)
    target.config.artifactory_ftp = 'files.example.com'
    target.is_current_target = lambda: True  # `update` only skips the cache for the target it names
    if cached:
        os.makedirs(target.dep.dep_dir, exist_ok=True)
        Path(target.dep.dep_dir, 'libfoo-pkg.zip').write_bytes(b'PK\x03\x04')
    return target


def _fetch(target, download):
    """Drive artifactory_fetch_and_reconfigure with the archive name fixed and the unzip stubbed."""
    with patch.object(art, 'artifactory_archive_name', return_value='libfoo-pkg'), \
         patch.object(art, '_fetch_package', **download) as fetch, \
         patch.object(art, 'unzip_and_load_target', return_value=(True, [])) as unzip:
        loaded, _ = art.artifactory_fetch_and_reconfigure(target)
    return loaded, fetch, unzip


# `update` on the current target skips the cache, which is what puts the run at the mercy of the download
_UPDATING = dict(update=True)


@pytest.mark.parametrize('download', [{'return_value': None},                   # offline: no exception
                                      {'side_effect': RuntimeError('404')}])    # the pkg-dep raise
def test_a_failed_download_falls_back_to_the_cached_archive(tmp_path, download):
    target = _target(tmp_path, cached=True, **_UPDATING)
    loaded, _, unzip = _fetch(target, download)
    assert loaded is True
    assert unzip.call_args.args[1].endswith('libfoo-pkg.zip')


@pytest.mark.parametrize('download, raises', [({'return_value': None}, False),
                                              ({'side_effect': RuntimeError('404')}, True)])
def test_a_failed_download_with_no_cache_still_reports_the_failure(tmp_path, download, raises):
    target = _target(tmp_path, cached=False, **_UPDATING)
    if raises:
        with pytest.raises(RuntimeError): _fetch(target, download)
    else:
        assert _fetch(target, download)[0] is False


def _corrupt_cache_run(tmp_path, download):
    """A cached zip that fails to unzip, so the run reaches the download with the cache already spent."""
    target = _target(tmp_path, cached=True)  # no update: the cache path runs first
    with patch.object(art, 'artifactory_archive_name', return_value='libfoo-pkg'), \
         patch.object(art, '_fetch_package', **download), \
         patch.object(art, 'unzip_and_load_target', return_value=(False, None)) as unzip:
        return art.artifactory_fetch_and_reconfigure(target), unzip


def test_a_corrupt_cache_is_not_retried_after_the_download_fails(tmp_path):
    # the run already unzipped it and it failed, so a second attempt would fail the same way
    (loaded, _), unzip = _corrupt_cache_run(tmp_path, {'return_value': None})
    assert loaded is False and unzip.call_count == 1


def test_a_download_error_still_raises_once_the_cache_is_spent(tmp_path):
    # nothing is left to fall back on, so the real error must reach the caller
    with pytest.raises(RuntimeError):
        _corrupt_cache_run(tmp_path, {'side_effect': RuntimeError('404')})
