"""
Tests for BuildTarget-level shim behavior:
- _require_source() refuses on a shim, allows on a clone
- _execute_deploy_tasks short-circuits on a shim without calling deploy()
"""
import os
import tempfile
import shutil
from unittest.mock import Mock, patch

from mama.build_dependency import BuildDependency
from mama.build_target import BuildTarget
from mama.types.git import Git


def _make_dep_and_target(tmpdir, as_shim: bool):
    config = Mock()
    config.artifactory_ftp = 'ftp.example.com'
    config.workspaces_root = tmpdir
    config.global_workspace = False
    config.platform_build_dir_name.return_value = 'linux'
    config.verbose = False
    config.print = False
    config.loaded_dependencies = {}
    # platform aliases for BuildTarget.__init__
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
    config.deploy = True
    config.upload = False
    config.no_target.return_value = False
    config.targets_all.return_value = False
    config.target_matches.return_value = True  # treat as current target

    git = Git(name='libfoo', url='https://example.com/libfoo.git',
              branch='main', tag='', mamafile=None, shallow=True, args=[])
    dep = BuildDependency(parent=None, config=config, workspace='packages', dep_source=git)
    dep.is_root = False
    dep.create_build_dir_if_needed()

    if as_shim:
        dep.write_shim_marker(archive_name='libfoo-linux-22-gcc11.3-x64-release-abc1234',
                              commit_hash='abc1234')

    target = BuildTarget(name='libfoo', config=config, dep=dep, args=[])
    return dep, target


def test_require_source_refuses_on_shim():
    tmpdir = tempfile.mkdtemp(prefix='mama_shim_test_')
    try:
        dep, target = _make_dep_and_target(tmpdir, as_shim=True)
        assert target._require_source('test') is False
    finally:
        shutil.rmtree(tmpdir)


def test_require_source_allows_on_non_shim():
    tmpdir = tempfile.mkdtemp(prefix='mama_shim_test_')
    try:
        dep, target = _make_dep_and_target(tmpdir, as_shim=False)
        assert target._require_source('test') is True
    finally:
        shutil.rmtree(tmpdir)


def test_execute_deploy_tasks_skips_deploy_for_shim():
    """A shim must not call user-defined deploy() or papa_upload_to()."""
    tmpdir = tempfile.mkdtemp(prefix='mama_shim_test_')
    try:
        dep, target = _make_dep_and_target(tmpdir, as_shim=True)
        # Replace deploy() with a sentinel that would fail the test if called.
        called = {'deploy': False, 'upload': False}

        def fake_deploy():
            called['deploy'] = True

        target.deploy = fake_deploy

        with patch('mama.build_target.papa_upload_to') as upload_mock:
            target._execute_deploy_tasks()
            upload_mock.assert_not_called()

        assert not called['deploy'], 'deploy() must not be invoked on a shim'
    finally:
        shutil.rmtree(tmpdir)


def test_execute_deploy_tasks_runs_deploy_for_non_shim():
    """Sanity check: non-shim still calls deploy()."""
    tmpdir = tempfile.mkdtemp(prefix='mama_shim_test_')
    try:
        dep, target = _make_dep_and_target(tmpdir, as_shim=False)
        called = {'deploy': False}

        def fake_deploy():
            called['deploy'] = True

        target.deploy = fake_deploy
        target._execute_deploy_tasks()
        assert called['deploy']
    finally:
        shutil.rmtree(tmpdir)
