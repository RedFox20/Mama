"""Tests for the `noart` shim-cache path on BuildDependency.

Bug background: `mama noart update all` used to fail mid-build for any dep
that was previously loaded as an artifactory shim (no source on disk, just a
papa.txt + libs unzipped into build_dir). The reason: `can_fetch_artifactory`
short-circuits to False under noart, which then skipped the shim probe AND
left the dep with no loaded exports - the build chain blew up downstream.

`noart` is supposed to mean "don't FETCH from artifactory", not "ignore my
local artifactory cache". The fix adds a separate path in `_load` that:
  1. Detects an existing shim marker.
  2. Probes the upstream commit via ls-remote (a cheap ref probe, not a
     package fetch - allowed under noart).
  3. If the stored hash still matches upstream → loads exports from the
     local papa.txt and proceeds normally.
  4. If upstream advanced → removes the stale marker so the regular git
     path can clone+build from source.

These tests pin that contract. The non-noart path is also exercised so
no regression sneaks in there.
"""
from __future__ import annotations

import os
import sys
import tempfile
import shutil
from unittest.mock import Mock, patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))
from mama.build_dependency import BuildDependency  # noqa: E402
from mama.types.git import Git  # noqa: E402


def _make_dep(tmpdir, disable_artifactory=False):
    config = Mock()
    config.artifactory_ftp = 'ftp.example.com'
    config.workspaces_root = tmpdir
    config.global_workspace = False
    config.platform_build_dir_name.return_value = 'linux'
    config.verbose = False
    config.print = False
    config.loaded_dependencies = {}
    config.target_matches.return_value = False
    config.disable_artifactory = disable_artifactory
    config.force_artifactory = False
    config.is_network_available.return_value = True

    git = Git(name='libfoo', url='https://example.com/libfoo.git',
              branch='main', tag='', mamafile=None, shallow=True, args=[])
    dep = BuildDependency(parent=None, config=config, workspace='packages', dep_source=git)
    dep.is_root = False
    dep.create_build_dir_if_needed()
    return dep


def _make_shim(tmpdir, disable_artifactory=False, stored_hash='abc1234'):
    dep = _make_dep(tmpdir, disable_artifactory=disable_artifactory)
    dep.write_shim_marker(
        archive_name=f'libfoo-linux-22-gcc11.3-x64-release-{stored_hash}',
        commit_hash=stored_hash,
    )
    # Write a believable papa.txt that artifactory_load_target can read.
    with open(os.path.join(dep.build_dir, 'papa.txt'), 'w') as f:
        f.write('p libfoo\nv 1.0\n')
    return dep


class TestNoartShimCacheHit:
    """noart + existing shim + upstream commit unchanged → load from cache."""

    def test_returns_target_when_hash_matches(self):
        tmpdir = tempfile.mkdtemp(prefix='mama_noart_test_')
        try:
            dep = _make_shim(tmpdir, disable_artifactory=True, stored_hash='abc1234')
            # ls-remote returns the same hash that's in the marker.
            with patch.object(Git, 'init_commit_hash', return_value='abc1234'), \
                 patch('mama.artifactory.artifactory_load_target',
                       return_value=(True, [])) as mock_load:
                target = dep.try_load_cached_shim()
            assert target is not None
            assert target.name == 'libfoo'
            # The load path must read from the local build_dir, NOT trigger any fetch.
            mock_load.assert_called_once()
            args, kwargs = mock_load.call_args
            assert args[1] == dep.build_dir  # deploy_path = build_dir
            # Marker still intact.
            assert dep.is_artifactory_shim()
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_shim_dependencies_are_added_as_children(self):
        tmpdir = tempfile.mkdtemp(prefix='mama_noart_test_')
        try:
            dep = _make_shim(tmpdir, disable_artifactory=True)
            child_dep_source = Mock(name='child')
            with patch.object(Git, 'init_commit_hash', return_value='abc1234'), \
                 patch('mama.artifactory.artifactory_load_target',
                       return_value=(True, [child_dep_source])), \
                 patch.object(BuildDependency, 'add_child') as mock_add_child:
                dep.try_load_cached_shim()
            mock_add_child.assert_called_once_with(child_dep_source)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


class TestNoartShimCacheStale:
    """noart + existing shim + upstream commit advanced → drop marker,
    return None so the git clone path takes over."""

    def test_stale_marker_is_removed(self):
        tmpdir = tempfile.mkdtemp(prefix='mama_noart_test_')
        try:
            dep = _make_shim(tmpdir, disable_artifactory=True, stored_hash='abc1234')
            assert dep.is_artifactory_shim()
            # ls-remote returns a different hash than what's stored.
            with patch.object(Git, 'init_commit_hash', return_value='def5678'), \
                 patch('mama.artifactory.artifactory_load_target') as mock_load:
                target = dep.try_load_cached_shim()
            assert target is None
            # Marker must be gone so the regular git path takes over next.
            assert not os.path.exists(dep.mama_shim_file())
            assert not dep.is_artifactory_shim()
            # We never tried to load from cache for a stale shim.
            mock_load.assert_not_called()
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


class TestNoartShimCacheMisses:
    """Defensive: degenerate marker / corrupted papa.txt / no marker at all
    must not crash and must return None."""

    def test_no_marker_returns_none(self):
        tmpdir = tempfile.mkdtemp(prefix='mama_noart_test_')
        try:
            dep = _make_dep(tmpdir, disable_artifactory=True)
            assert not dep.is_artifactory_shim()
            assert dep.try_load_cached_shim() is None
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_marker_without_hash_returns_none(self):
        tmpdir = tempfile.mkdtemp(prefix='mama_noart_test_')
        try:
            dep = _make_dep(tmpdir, disable_artifactory=True)
            # Write a marker without the 'hash' field.
            with open(dep.mama_shim_file(), 'w') as f:
                f.write('shim 1\nname libfoo\n')
            dep._is_shim_cache = None  # invalidate the cached flag
            assert dep.try_load_cached_shim() is None
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_ls_remote_failure_does_not_drop_marker(self):
        """ls-remote returning None (e.g. network down) must leave the marker
        intact - we shouldn't penalize a transient network issue by forcing
        a full re-clone next time."""
        tmpdir = tempfile.mkdtemp(prefix='mama_noart_test_')
        try:
            dep = _make_shim(tmpdir, disable_artifactory=True, stored_hash='abc1234')
            with patch.object(Git, 'init_commit_hash', return_value=None), \
                 patch('mama.artifactory.artifactory_load_target',
                       return_value=(True, [])):
                target = dep.try_load_cached_shim()
            # ls-remote failed → we treat the cache as fresh (couldn't prove stale).
            assert target is not None
            assert dep.is_artifactory_shim()  # marker preserved
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_corrupted_papa_returns_none(self):
        """artifactory_load_target failing (e.g. papa.txt missing or wrong
        project_name) → cache cannot be honoured, return None."""
        tmpdir = tempfile.mkdtemp(prefix='mama_noart_test_')
        try:
            dep = _make_shim(tmpdir, disable_artifactory=True, stored_hash='abc1234')
            with patch.object(Git, 'init_commit_hash', return_value='abc1234'), \
                 patch('mama.artifactory.artifactory_load_target',
                       return_value=(False, None)):
                assert dep.try_load_cached_shim() is None
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


class TestNonNoartRegression:
    """Critical: `mama update all` (no noart) must NOT exercise the new
    cached-shim path. The regular probe (try_load_artifactory_shim) handles
    refreshes from artifactory."""

    def test_load_without_noart_does_not_call_cached_shim_path(self):
        """In non-noart mode, the `_load` flow should run try_load_artifactory_shim
        for a shim'd dep, NOT try_load_cached_shim. We assert by checking which
        code path is entered."""
        tmpdir = tempfile.mkdtemp(prefix='mama_noart_test_')
        try:
            dep = _make_shim(tmpdir, disable_artifactory=False, stored_hash='abc1234')
            dep.config.update = False
            dep.config.build = False
            dep.config.clean = False
            dep.config.rebuild = False
            dep.config.run_cmake_configure = False
            dep.config.target = None
            dep.config.list = False

            # Stub everything _load might do downstream so we can isolate the choice
            # between the two probe paths.
            with patch.object(BuildDependency, 'try_load_cached_shim') as mock_cached, \
                 patch('mama.build_dependency.try_load_artifactory_shim',
                       return_value=(None, None)) as mock_probe, \
                 patch.object(BuildDependency, '_load_target'), \
                 patch.object(BuildDependency, '_should_build', return_value=False), \
                 patch.object(BuildDependency, 'can_fetch_artifactory', return_value=True), \
                 patch.object(BuildDependency, 'should_load_artifactory', return_value=False), \
                 patch.object(BuildDependency, 'load_build_products'):
                # dep.target must be set so _should_build sees something
                dep.target = Mock(args=[], settings=Mock(), dependencies=Mock(),
                                  build_products=[])
                dep._load()
            # Non-noart: the new cached path must NOT be called.
            mock_cached.assert_not_called()
            # The regular probe SHOULD be called.
            mock_probe.assert_called_once()
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_noart_routes_to_cached_shim_path(self):
        """Symmetry test: with noart, the cached path IS called and the
        regular probe is NOT."""
        tmpdir = tempfile.mkdtemp(prefix='mama_noart_test_')
        try:
            dep = _make_shim(tmpdir, disable_artifactory=True, stored_hash='abc1234')
            dep.config.update = False
            dep.config.build = False
            dep.config.clean = False
            dep.config.rebuild = False
            dep.config.run_cmake_configure = False
            dep.config.target = None
            dep.config.list = False

            fake_target = Mock(args=[], settings=Mock(), dependencies=Mock(),
                               build_products=[])
            with patch.object(BuildDependency, 'try_load_cached_shim',
                              return_value=fake_target) as mock_cached, \
                 patch('mama.build_dependency.try_load_artifactory_shim') as mock_probe, \
                 patch.object(BuildDependency, '_load_target', return_value=fake_target), \
                 patch.object(BuildDependency, '_should_build', return_value=False), \
                 patch.object(BuildDependency, 'should_load_artifactory', return_value=False), \
                 patch.object(BuildDependency, 'load_build_products'):
                dep._load()
            mock_cached.assert_called_once()
            mock_probe.assert_not_called()
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
