"""Pins `if_release_br_or_tag`: an upload needs the root checkout and every git dep on a release source."""
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from testutils import git_init_commit, git_run
from mama import papa_upload
from mama.papa_upload import _release_tree_can_publish


def _dep(name, branch='', tag='', children=()):
    source = SimpleNamespace(is_git=True, branch=branch, tag=tag, url=f'/no/such/remote/{name}')
    return SimpleNamespace(name=name, dep_source=source, get_children=lambda: list(children))


def _target(root=None, dep=None, guard=True):
    config = SimpleNamespace(root_source_dir=str(root) if root else None, git_timeout=10, if_release_br_or_tag=guard)
    return SimpleNamespace(name='libfoo', config=config, dep=dep or _dep('libfoo'))


def _clone_of(tmp_path, default_branch):
    """A root checkout whose `origin` names `default_branch` as its HEAD."""
    git_init_commit(tmp_path / 'remote', branch=default_branch)
    git_run(['clone', '-q', str(tmp_path / 'remote'), str(tmp_path / 'root')], tmp_path)
    return tmp_path / 'root'


def test_without_the_option_a_feature_branch_publishes(tmp_path):
    git_init_commit(tmp_path, branch='feat/json')
    assert _release_tree_can_publish(_target(tmp_path, dep=_dep('libfoo', branch='feat/json'), guard=False))


@pytest.mark.parametrize('branch, publishes', [('release/1.2', True), ('feat/json', False), ('develop', False)])
def test_the_root_checkout_publishes_only_from_a_release_branch(tmp_path, branch, publishes):
    git_init_commit(tmp_path, branch=branch)  # no origin, so no default branch either
    assert _release_tree_can_publish(_target(tmp_path)) is publishes


@pytest.mark.parametrize('default', ['develop', 'kratt'])
def test_the_root_checkout_publishes_from_the_default_branch_of_origin(tmp_path, default):
    assert _release_tree_can_publish(_target(_clone_of(tmp_path, default)))


def test_a_detached_root_publishes_only_from_a_tag(tmp_path):
    git_init_commit(tmp_path, branch='release/1.0')
    git_run(['checkout', '-q', '--detach'], tmp_path)
    assert not _release_tree_can_publish(_target(tmp_path))
    git_run(['tag', 'v1.0'], tmp_path)
    assert _release_tree_can_publish(_target(tmp_path))


def test_a_root_outside_git_cannot_tell_and_publishes(tmp_path):
    assert _release_tree_can_publish(_target(tmp_path))


@pytest.mark.parametrize('branch, tag, publishes', [('', '', True), ('release/2.0', '', True), ('', 'v1.6.1', True),
                                                    ('', 'caf5158', True), ('feat/json', '', False),
                                                    ('feat/json', 'v1.5.0', False), ('develop', '', False)])
def test_a_git_dep_anywhere_in_the_tree_needs_a_release_source(branch, tag, publishes):
    leaf = _dep('krattutil', branch=branch, tag=tag)
    tree = _dep('rtpvideo', children=[_dep('mavpacker', children=[leaf])])
    assert _release_tree_can_publish(_target(dep=tree)) is publishes


def test_the_default_branch_of_the_remote_is_a_release_branch(tmp_path):
    git_init_commit(tmp_path, branch='kratt')
    dep = _dep('ardupilot', branch='kratt')
    dep.dep_source.url = str(tmp_path)
    assert _release_tree_can_publish(_target(dep=dep))


def test_a_refused_tree_uploads_nothing(tmp_path):
    with patch.object(papa_upload, '_release_tree_can_publish', autospec=True, return_value=False), \
         patch.object(papa_upload, '_instrumented_build_can_publish', autospec=True, return_value=True), \
         patch.object(papa_upload, 'artifactory_upload_ftp', autospec=True) as upload:
        papa_upload.papa_upload_to(_target(), str(tmp_path))
    upload.assert_not_called()


def test_a_root_whose_git_does_not_answer_skips_the_upload(tmp_path):
    git_init_commit(tmp_path, branch='release/1.0')
    with patch.object(papa_upload, 'execute_piped', autospec=True, return_value=None):
        assert not _release_tree_can_publish(_target(tmp_path))


def test_the_option_parses_from_the_command_line():
    from mama.build_config import BuildConfig
    assert BuildConfig(['upload', 'if_release_br_or_tag']).if_release_br_or_tag
    assert not BuildConfig(['upload']).if_release_br_or_tag
