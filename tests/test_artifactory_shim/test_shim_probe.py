"""
Unit tests for the artifactory shim probe path.

These tests exercise `try_load_artifactory_shim` and the surrounding gating
without contacting any real server or git remote. The fetch+unzip+load chain
is stubbed at `artifactory_fetch_and_reconfigure`.
"""
import os
import tempfile
import shutil
from unittest.mock import patch, Mock

import mama.artifactory as artifactory_mod
from mama.artifactory import try_load_artifactory_shim
from mama.build_dependency import BuildDependency
from mama.types.git import Git


def _make_dep(tmpdir, artifactory_ftp='ftp.example.com'):
    config = Mock()
    config.artifactory_ftp = artifactory_ftp
    config.workspaces_root = tmpdir
    config.global_workspace = False
    config.platform_build_dir_name.return_value = 'linux'
    config.verbose = False
    config.print = False
    config.loaded_dependencies = {}
    config.target_matches.return_value = False
    # used inside BuildTarget.__init__ via _update_platform_aliases
    config.msvc = False
    config.linux = True
    config.macos = False
    config.ios = False
    config.android = None
    config.raspi = False
    config.oclea = None
    config.xilinx = None
    config.mips = None
    config.imx8mp = None
    config.yocto_linux = None
    config.debug = False
    config.prefer_ninja = False
    config.ninja_path = ''
    config.cmake_command = 'cmake'
    # needed by artifactory_archive_name
    config.get_distro_info.return_value = ('ubuntu', 22, 4)
    config.compiler_version.return_value = 'gcc11.3'
    config.arch = 'x64'
    config.release = True
    config.sanitize = None
    config.sanitizer_suffix.return_value = ''
    config.update = False

    git = Git(name='libfoo', url='https://example.com/libfoo.git',
              branch='main', tag='', mamafile=None, shallow=True, args=[])
    dep = BuildDependency(parent=None, config=config, workspace='packages', dep_source=git)
    dep.is_root = False
    dep.create_build_dir_if_needed()
    return dep, config, git


def test_shim_probe_no_artifactory_returns_none():
    """When artifactory_ftp is unset, the shim probe must be a no-op."""
    tmpdir = tempfile.mkdtemp(prefix='mama_shim_test_')
    try:
        dep, _, _ = _make_dep(tmpdir, artifactory_ftp=None)
        target, deps = try_load_artifactory_shim(dep)
        assert target is None
        assert deps is None
        # marker never written
        assert not os.path.exists(dep.mama_shim_file())
    finally:
        shutil.rmtree(tmpdir)


def test_shim_probe_unresolvable_hash_returns_none():
    """If ls-remote / cache / .git all fail, init_commit_hash returns None and
    the probe must bail without touching state."""
    tmpdir = tempfile.mkdtemp(prefix='mama_shim_test_')
    try:
        dep, _, git = _make_dep(tmpdir)
        with patch.object(Git, 'init_commit_hash', return_value=None):
            target, deps = try_load_artifactory_shim(dep)
        assert target is None
        assert deps is None
        assert not os.path.exists(dep.mama_shim_file())
        assert not dep.from_artifactory
    finally:
        shutil.rmtree(tmpdir)


def test_shim_probe_fetch_fails_returns_none_and_clears_state():
    """When artifactory_fetch_and_reconfigure returns (False, None), the probe
    must reset from_artifactory so the clone path can run cleanly."""
    tmpdir = tempfile.mkdtemp(prefix='mama_shim_test_')
    try:
        dep, _, _ = _make_dep(tmpdir)
        with patch.object(Git, 'init_commit_hash', return_value='abc1234'), \
             patch.object(artifactory_mod, 'artifactory_fetch_and_reconfigure',
                          return_value=(False, None)) as fetch_mock:
            target, deps = try_load_artifactory_shim(dep)
        assert target is None
        assert deps is None
        assert not os.path.exists(dep.mama_shim_file())
        assert not dep.from_artifactory
        fetch_mock.assert_called_once()
    finally:
        shutil.rmtree(tmpdir)


def test_shim_probe_fetch_succeeds_writes_marker():
    """On fetch success, the probe must return a target + deps and persist a marker."""
    tmpdir = tempfile.mkdtemp(prefix='mama_shim_test_')
    try:
        dep, _, _ = _make_dep(tmpdir)
        fake_deps = ['some_dep_source_placeholder']

        def fake_fetch(probe_target):
            # mimic artifactory_load_target's side effect on the dep:
            probe_target.dep.from_artifactory = True
            probe_target.exported_includes = ['/fake/include']
            return (True, fake_deps)

        with patch.object(Git, 'init_commit_hash', return_value='abc1234'), \
             patch.object(artifactory_mod, 'artifactory_fetch_and_reconfigure',
                          side_effect=fake_fetch):
            target, deps = try_load_artifactory_shim(dep)

        assert target is not None
        assert deps is fake_deps
        assert target.exported_includes == ['/fake/include']
        # marker persisted
        marker = dep.read_shim_marker()
        assert marker['hash'] == 'abc1234'
        assert marker['url'] == 'https://example.com/libfoo.git'
        assert dep.is_artifactory_shim()
    finally:
        shutil.rmtree(tmpdir)


def test_shim_probe_uses_resolved_hash_not_tag():
    """The probe must call init_commit_hash; for a non-hex tag this triggers
    ls-remote internally. We assert the hash threaded through to the marker."""
    tmpdir = tempfile.mkdtemp(prefix='mama_shim_test_')
    try:
        dep, _, _ = _make_dep(tmpdir)

        def fake_fetch(probe_target):
            probe_target.dep.from_artifactory = True
            return (True, [])

        with patch.object(Git, 'init_commit_hash', return_value='def5678') as hash_mock, \
             patch.object(artifactory_mod, 'artifactory_fetch_and_reconfigure',
                          side_effect=fake_fetch):
            target, _ = try_load_artifactory_shim(dep)

        assert target is not None
        hash_mock.assert_called_once()
        # use_cache=True, fetch_remote=True per Phase 1 contract
        args, kwargs = hash_mock.call_args
        assert kwargs.get('use_cache') is True
        assert kwargs.get('fetch_remote') is True

        marker = dep.read_shim_marker()
        assert marker['hash'] == 'def5678'
    finally:
        shutil.rmtree(tmpdir)


def test_shim_probe_skipped_for_non_git_dep():
    """Local / pkg deps must never enter the shim probe path."""
    tmpdir = tempfile.mkdtemp(prefix='mama_shim_test_')
    try:
        dep, _, _ = _make_dep(tmpdir)
        # mutate dep_source to look non-git
        dep.dep_source.is_git = False

        with patch.object(Git, 'init_commit_hash') as hash_mock, \
             patch.object(artifactory_mod, 'artifactory_fetch_and_reconfigure') as fetch_mock:
            target, deps = try_load_artifactory_shim(dep)

        assert target is None
        assert deps is None
        hash_mock.assert_not_called()
        fetch_mock.assert_not_called()
    finally:
        shutil.rmtree(tmpdir)
