"""
Unit tests for the artifactory shim marker file.

The marker (`{build_dir}/mama_shim`) is the persistent source of truth for
'this dep was loaded from artifactory without a clone'. These tests verify
the roundtrip and detection helpers without spinning up any real BuildTarget.
"""
import os
import tempfile
import shutil
from unittest.mock import Mock

from mama.build_dependency import BuildDependency, MAMA_SHIM_FILENAME
from mama.types.git import Git


def _make_dep_in_tempdir():
    """Construct a real BuildDependency wired to a temp workspace, no clone."""
    tmpdir = tempfile.mkdtemp(prefix='mama_shim_test_')
    config = Mock()
    config.artifactory_ftp = None
    config.workspaces_root = tmpdir
    config.global_workspace = False
    config.platform_build_dir_name.return_value = 'linux'
    config.verbose = False
    config.print = False
    config.loaded_dependencies = {}

    git = Git(name='libfoo', url='https://example.com/libfoo.git',
              branch='main', tag='', mamafile=None, shallow=True, args=[])
    dep = BuildDependency(parent=None, config=config, workspace='packages', dep_source=git)
    dep.is_root = False  # the constructor sets is_root from parent=None; override for tests
    dep.create_build_dir_if_needed()
    return dep, tmpdir


def test_no_marker_means_not_shim():
    dep, tmpdir = _make_dep_in_tempdir()
    try:
        assert not dep.is_artifactory_shim()
        assert not dep.is_real_clone()
    finally:
        shutil.rmtree(tmpdir)


def test_write_then_detect_shim():
    dep, tmpdir = _make_dep_in_tempdir()
    try:
        dep.write_shim_marker(archive_name='libfoo-linux-22-gcc11.3-x64-release-abc1234',
                              commit_hash='abc1234')
        assert os.path.exists(dep.mama_shim_file())
        assert dep.is_artifactory_shim()
        assert not dep.is_real_clone()
    finally:
        shutil.rmtree(tmpdir)


def test_shim_marker_roundtrip():
    dep, tmpdir = _make_dep_in_tempdir()
    try:
        dep.write_shim_marker(archive_name='libfoo-linux-22-gcc11.3-x64-release-abc1234',
                              commit_hash='abc1234')
        data = dep.read_shim_marker()
        assert data['name'] == 'libfoo'
        assert data['url'] == 'https://example.com/libfoo.git'
        assert data['branch'] == 'main'
        assert data['tag'] == ''
        assert data['hash'] == 'abc1234'
        assert data['archive'] == 'libfoo-linux-22-gcc11.3-x64-release-abc1234'
    finally:
        shutil.rmtree(tmpdir)


def test_remove_shim_marker_is_idempotent():
    dep, tmpdir = _make_dep_in_tempdir()
    try:
        dep.write_shim_marker(archive_name='x', commit_hash='y')
        assert os.path.exists(dep.mama_shim_file())
        dep.remove_shim_marker()
        assert not os.path.exists(dep.mama_shim_file())
        # second remove should not raise
        dep.remove_shim_marker()
    finally:
        shutil.rmtree(tmpdir)


def test_real_clone_takes_precedence_over_shim():
    """If both .git and mama_shim are present, is_artifactory_shim is False."""
    dep, tmpdir = _make_dep_in_tempdir()
    try:
        dep.write_shim_marker(archive_name='x', commit_hash='y')
        # fake a .git directory in src_dir to simulate a real clone
        os.makedirs(dep.src_dir, exist_ok=True)
        os.makedirs(os.path.join(dep.src_dir, '.git'), exist_ok=True)
        assert dep.is_real_clone()
        assert not dep.is_artifactory_shim()
    finally:
        shutil.rmtree(tmpdir)


def test_shim_filename_constant():
    """Hardcode-check the marker filename. The defense-in-depth check in
    papa_deploy_to relies on the literal 'mama_shim'."""
    assert MAMA_SHIM_FILENAME == 'mama_shim'
