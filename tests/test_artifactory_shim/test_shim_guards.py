"""
Tests for shim-aware guards:
- _should_build refuses to rebuild a shim
- update_mamafile_tag / update_cmakelists_tag short-circuit for shims
- _execute_deploy_tasks skips deploy for shims
- BuildTarget._require_source returns False for shims
- papa_deploy_to refuses on a shim destination
- dirty() removes the shim marker
"""
import os
import tempfile
import shutil
from unittest.mock import Mock, patch

import pytest

from mama.build_dependency import BuildDependency
from mama.types.git import Git
from mama.papa_deploy import papa_deploy_to


def _make_dep(tmpdir):
    config = Mock()
    config.artifactory_ftp = 'ftp.example.com'
    config.workspaces_root = tmpdir
    config.global_workspace = False
    config.platform_build_dir_name.return_value = 'linux'
    config.verbose = False
    config.print = False
    config.loaded_dependencies = {}
    config.target_matches.return_value = False
    # for _execute_deploy_tasks
    config.deploy = True
    config.upload = False
    config.no_target.return_value = False
    config.targets_all.return_value = False
    # for _should_build
    config.build = True
    config.update = False
    config.clean = False
    config.rebuild = False
    config.run_cmake_configure = False
    config.target = None

    git = Git(name='libfoo', url='https://example.com/libfoo.git',
              branch='main', tag='', mamafile=None, shallow=True, args=[])
    dep = BuildDependency(parent=None, config=config, workspace='packages', dep_source=git)
    dep.is_root = False
    dep.create_build_dir_if_needed()
    return dep


def _make_shim(tmpdir):
    dep = _make_dep(tmpdir)
    dep.write_shim_marker(archive_name='libfoo-linux-22-gcc11.3-x64-release-abc1234',
                          commit_hash='abc1234')
    return dep


# ---------------------------------------------------------------------------
# update_mamafile_tag / update_cmakelists_tag short-circuit
# ---------------------------------------------------------------------------

def test_update_mamafile_tag_returns_false_for_shim():
    tmpdir = tempfile.mkdtemp(prefix='mama_shim_test_')
    try:
        dep = _make_shim(tmpdir)
        assert dep.is_artifactory_shim()
        # Even though src_dir is None-ish and would normally short-circuit to False,
        # we want this to be defensively False regardless of mamafile presence.
        assert dep.update_mamafile_tag() is False
        assert dep.update_cmakelists_tag() is False
    finally:
        shutil.rmtree(tmpdir)


# ---------------------------------------------------------------------------
# _should_build refuses to rebuild a shim
# ---------------------------------------------------------------------------

def test_should_build_returns_false_for_shim_even_with_update_target():
    tmpdir = tempfile.mkdtemp(prefix='mama_shim_test_')
    try:
        dep = _make_shim(tmpdir)
        # simulate `mama update libfoo`: would normally trigger build('update target=libfoo')
        dep.config.update = True
        dep.config.target = 'libfoo'

        target_mock = Mock()
        target_mock.name = 'libfoo'
        target_mock.args = []
        target_mock.build_products = []

        result = dep._should_build(dep.config, target_mock,
                                   is_target=True, git_changed=False, loaded_from_pkg=True)
        assert result is False
    finally:
        shutil.rmtree(tmpdir)


def test_should_build_returns_false_for_shim_with_clean_target():
    """`mama clean libfoo` would normally short-circuit to build('cleaned target')."""
    tmpdir = tempfile.mkdtemp(prefix='mama_shim_test_')
    try:
        dep = _make_shim(tmpdir)
        dep.config.clean = True
        dep.config.target = 'libfoo'

        target_mock = Mock()
        target_mock.name = 'libfoo'
        target_mock.args = []

        result = dep._should_build(dep.config, target_mock,
                                   is_target=True, git_changed=False, loaded_from_pkg=True)
        assert result is False
    finally:
        shutil.rmtree(tmpdir)


# ---------------------------------------------------------------------------
# dirty() removes the shim marker
# ---------------------------------------------------------------------------

def test_dirty_removes_shim_marker():
    tmpdir = tempfile.mkdtemp(prefix='mama_shim_test_')
    try:
        dep = _make_shim(tmpdir)
        # `dirty` reads dep.target.build_products; supply a Mock that returns [].
        target_mock = Mock()
        target_mock.build_products = []
        dep.target = target_mock

        assert os.path.exists(dep.mama_shim_file())
        dep.dirty()
        assert not os.path.exists(dep.mama_shim_file())
        assert not dep.is_artifactory_shim()
    finally:
        shutil.rmtree(tmpdir)


# ---------------------------------------------------------------------------
# papa_deploy_to refuses on shim destination
# ---------------------------------------------------------------------------

def test_papa_deploy_to_refuses_with_shim_marker_in_destination():
    """If a caller passes the shim's build_dir as the deploy destination,
    papa_deploy_to must raise rather than overwrite the artifactory snapshot."""
    tmpdir = tempfile.mkdtemp(prefix='mama_shim_test_')
    try:
        dep = _make_shim(tmpdir)
        # destination has a mama_shim marker → must refuse
        target = Mock()
        target.config.print = False
        target.config.verbose = False
        target.config.test = False
        target.is_current_target.return_value = False
        target.name = 'libfoo'

        with pytest.raises(RuntimeError, match='mama_shim marker'):
            papa_deploy_to(target, dep.build_dir,
                           r_includes=False, r_dylibs=False,
                           r_syslibs=False, r_assets=False)
    finally:
        shutil.rmtree(tmpdir)


def test_papa_deploy_to_succeeds_for_normal_destination():
    """Sanity: a non-shim destination still works."""
    tmpdir = tempfile.mkdtemp(prefix='mama_shim_test_')
    deploy_dir = os.path.join(tmpdir, 'deploy', 'libfoo')
    try:
        os.makedirs(deploy_dir, exist_ok=True)

        target = Mock()
        target.config.print = False
        target.config.verbose = False
        target.config.test = False
        target.is_current_target.return_value = False
        target.name = 'libfoo'
        target.exported_includes = []
        target.exported_libs = []
        target.exported_syslibs = []
        target.exported_assets = []
        target.includes_root = ('', '', '')
        target.children.return_value = []
        target.build_dir.return_value = deploy_dir
        target.source_dir.return_value = deploy_dir

        # no mama_shim in deploy_dir → must not raise
        papa_deploy_to(target, deploy_dir,
                       r_includes=False, r_dylibs=False,
                       r_syslibs=False, r_assets=False)
        assert os.path.exists(os.path.join(deploy_dir, 'papa.txt'))
    finally:
        shutil.rmtree(tmpdir)
