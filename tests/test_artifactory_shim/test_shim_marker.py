"""Shim marker file: roundtrip, detection, real-clone precedence."""
import os

from testutils import make_mock_dep

from mama.util import MAMA_SHIM_FILENAME


def test_no_marker_means_not_shim(tmp_path):
    dep = make_mock_dep(tmp_path, artifactory_ftp=None)
    assert not dep.is_artifactory_shim()
    assert not dep.is_real_clone()


def test_write_then_detect_shim(tmp_path):
    dep = make_mock_dep(tmp_path, artifactory_ftp=None)
    dep.write_shim_marker(archive_name='libfoo-linux-22-gcc11.3-x64-release-abc1234',
                          commit_hash='abc1234')
    assert os.path.exists(dep.mama_shim_file())
    assert dep.is_artifactory_shim()
    assert not dep.is_real_clone()


def test_shim_marker_roundtrip(tmp_path):
    dep = make_mock_dep(tmp_path, artifactory_ftp=None)
    dep.write_shim_marker(archive_name='libfoo-linux-22-gcc11.3-x64-release-abc1234',
                          commit_hash='abc1234')
    data = dep.read_shim_marker()
    assert data['name'] == 'libfoo'
    assert data['url'] == 'https://example.com/libfoo.git'
    assert data['branch'] == 'main'
    assert data['tag'] == ''
    assert data['hash'] == 'abc1234'
    assert data['archive'] == 'libfoo-linux-22-gcc11.3-x64-release-abc1234'


def test_remove_shim_marker_is_idempotent(tmp_path):
    dep = make_mock_dep(tmp_path, artifactory_ftp=None)
    dep.write_shim_marker(archive_name='x', commit_hash='y')
    dep.remove_shim_marker()
    assert not os.path.exists(dep.mama_shim_file())
    dep.remove_shim_marker()  # must not raise


def test_real_clone_takes_precedence_over_shim(tmp_path):
    # is_artifactory_shim() must be False if both .git and the marker exist.
    dep = make_mock_dep(tmp_path, artifactory_ftp=None)
    dep.write_shim_marker(archive_name='x', commit_hash='y')
    os.makedirs(os.path.join(dep.src_dir, '.git'), exist_ok=True)
    assert dep.is_real_clone()
    assert not dep.is_artifactory_shim()


def test_shim_filename_constant():
    # papa_deploy_to's defense-in-depth check relies on the literal 'mama_shim'.
    assert MAMA_SHIM_FILENAME == 'mama_shim'
