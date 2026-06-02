"""Pins `https-override` / `ssh-override`: ssh<->https url rewriting and that a
protocol-only override is not treated as a url change (no spurious wipe)."""
from unittest.mock import patch

import pytest
from testutils import make_mock_dep

from mama.types.git import Git, convert_git_url, same_git_remote

GH_SSH = 'git@github.com:KrattWorks/mavlink-headers.git'
GH_HTTPS = 'https://github.com/KrattWorks/mavlink-headers.git'


@pytest.mark.parametrize('url,expected', [
    (GH_SSH, GH_HTTPS),
    (GH_HTTPS, GH_HTTPS),  # already https
    ('ssh://git@example.com:2222/group/repo.git', 'https://example.com/group/repo.git'),  # port dropped
    ('git@gitlab.com:grp/sub/repo.git', 'https://gitlab.com/grp/sub/repo.git'),  # nested groups
    ('/srv/git/repo.git', '/srv/git/repo.git'),  # local path untouched
    ('file:///srv/git/repo.git', 'file:///srv/git/repo.git'),
    ('C:/repos/repo.git', 'C:/repos/repo.git'),  # windows local path
])
def test_to_https(url, expected):
    assert convert_git_url(url, 'https') == expected


@pytest.mark.parametrize('url,expected', [
    (GH_HTTPS, GH_SSH),
    (GH_SSH, GH_SSH),  # already ssh
    ('https://token@github.com/RedFox20/ReCpp.git', 'git@github.com:RedFox20/ReCpp.git'),  # creds dropped
    ('https://gitlab.com/grp/sub/repo.git', 'git@gitlab.com:grp/sub/repo.git'),
    ('/srv/git/repo.git', '/srv/git/repo.git'),
])
def test_to_ssh(url, expected):
    assert convert_git_url(url, 'ssh') == expected


def test_same_remote_ignores_protocol_creds_and_suffix():
    assert same_git_remote(GH_SSH, GH_HTTPS)
    assert same_git_remote('https://token@github.com/x/y', 'git@github.com:x/y.git')
    assert not same_git_remote(GH_HTTPS, 'https://github.com/KrattWorks/other.git')


def test_apply_url_override_rewrites_dep_url(tmp_path):
    dep = make_mock_dep(tmp_path, url=GH_SSH, git_url_override='https')
    assert dep.dep_source.url == GH_HTTPS
    assert dep.dep_source.url_overridden


def test_no_override_leaves_url(tmp_path):
    dep = make_mock_dep(tmp_path, url=GH_SSH, git_url_override=None)
    assert dep.dep_source.url == GH_SSH
    assert not dep.dep_source.url_overridden


def test_check_status_override_is_not_url_change(tmp_path):
    """Stored ssh url vs overridden https url is the same repo -> no wipe."""
    dep = make_mock_dep(tmp_path, url=GH_SSH, git_url_override='https')
    git: Git = dep.dep_source
    stored = (GH_SSH, '', 'main', 'abc1234')
    with patch.object(git, 'read_stored_status', return_value=stored), \
         patch.object(git, 'fetch_origin'), \
         patch.object(git, 'get_commit_hash', return_value='abc1234'):
        assert git.check_status(dep) is False
    assert not git.url_changed
