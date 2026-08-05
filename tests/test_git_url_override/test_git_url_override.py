"""Pins git.py helpers: ssh<->https url rewriting (protocol-only override is not a url change, no
spurious wipe) and the update-output noise filter."""
import contextlib
from unittest.mock import Mock, patch

import pytest
from testutils import make_mock_dep

from mama.types.git import Git, convert_git_url, same_git_remote, _is_git_status_noise
from mama.utils.progress import git_progress_status

GH_SSH = 'git@github.com:example/mavlink-headers.git'
GH_HTTPS = 'https://github.com/example/mavlink-headers.git'


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
    assert not same_git_remote(GH_HTTPS, 'https://github.com/example/other.git')


def test_apply_url_override_rewrites_dep_url(tmp_path):
    dep = make_mock_dep(tmp_path, url=GH_SSH, git_url_override='https')
    assert dep.dep_source.url == GH_HTTPS
    assert dep.dep_source.url_overridden


def test_no_override_leaves_url(tmp_path):
    dep = make_mock_dep(tmp_path, url=GH_SSH, git_url_override=None)
    assert dep.dep_source.url == GH_SSH
    assert not dep.dep_source.url_overridden


@pytest.mark.parametrize('line', [
    "Reset branch 'main'", "branch 'main' set up to track 'origin/main'.",
    "Your branch is up to date with 'origin/main'.", 'Already up to date.', "Switched to branch 'main'",
    'HEAD is now at 98f23d8 QCoro 0.13.0',  # post reset/checkout chatter from a parallel-mode git checkout
    "Your configuration specifies to merge with the ref 'refs/heads/x'", 'from the remote, but no such ref was fetched.',
    '/etc/ssh/ssh_config line 53: Unsupported option "gssapiauthentication"',  # ssh built without GSSAPI
    '/home/ci/.ssh/config line 7: Unsupported option "gssapikeyexchange"',
    # transfer bookkeeping: a percent-less counter, the pack total, the ref update and the submodule report
    'remote: Enumerating objects: 12, done.', 'remote: Total 11 (delta 8), reused 2 (delta 0)',
    'From https://github.com/RedFox20/ReCpp', ' * branch  caf5158 -> FETCH_HEAD',
    ' * [new branch] main -> origin/main', "Cloning into '/w/packages/SDL/SDL'...",
    "Submodule 'vcpkg' (https://github.com/microsoft/vcpkg.git) registered for path 'vcpkg'",
    "Submodule path 'vcpkg': checked out '7213cf8'"])
def test_update_noise_is_filtered(line):
    assert _is_git_status_noise(line)


@pytest.mark.parametrize('line', [
    'error: pathspec broke', "fatal: couldn't find remote ref x",
    'ControlSocket /home/ci/.ssh/cm/99ac79e already exists, disabling multiplexing',  # a real multiplex fault
    'mux_client_request_session: session request failed: Session open refused by peer'])
def test_real_git_output_is_kept(line):
    assert not _is_git_status_noise(line)


def test_git_progress_status_classifies_transfer_lines():
    assert git_progress_status('Receiving objects:  42% (5/12)') == ('receiving objects  ', 42)
    assert git_progress_status('remote: Counting objects: 100% (30/30), done.')[1] == 100
    assert git_progress_status(' * [new branch] main -> origin/main') is None  # a real ref line, not progress
    assert git_progress_status('From https://github.com/RedFox20/ReCpp') is None


@pytest.mark.parametrize('line,percent', [
    ('Unpacking objects:  45% (5/11)', 45), ('remote: Compressing objects:  64% (16/25)', 64),
    ('Checking out files:  90% (900/1000)', 90), ('Filtering content:  12% (1/8)', 12),
    ('remote: Enumerating objects: 100% (21/21), done.', 100)])
def test_every_git_counter_is_classified_as_progress(line, percent):
    assert git_progress_status(line)[1] == percent  # a missed counter reaches the terminal once per percent


def test_a_counter_without_a_percent_is_not_progress():
    assert git_progress_status('remote: Enumerating objects: 21, done.') is None  # chatter, see the noise filter


def test_is_progress_line_matches_git_and_download_bars():
    from mama.utils.progress import is_progress_line
    assert is_progress_line('remote: Counting objects:  10% (29/290)')  # git clone output
    assert is_progress_line('  ReCpp  receiving objects  42%')          # mama's collapsed git redraw
    assert is_progress_line('  |=====<-----|  42% (1.2s)')              # mama's artifactory download bar
    assert is_progress_line('x264  45% [====>      ] 3.4MB/s')          # a custom downloader's bar
    assert not is_progress_line('remote: Enumerating objects: 290, done.')  # no %, kept verbatim
    assert not is_progress_line('[3/74] Building CXX object foo.cpp.o')     # a real build line


def test_run_git_collapses_progress_flood_but_keeps_real_lines(tmp_path):
    # run_git must collapse the per-percent progress lines instead of printing every one raw
    dep = make_mock_dep(tmp_path, print=True)
    flood = [f'Receiving objects: {p}% ({p}/100)' for p in range(101)]
    real = ['From https://github.com/RedFox20/ReCpp', ' * [new branch] main -> origin/main',
            'warning: the repository is shallow']
    consoled, progressed = [], []
    def fake_run(cmd, io_func=None, **kw):
        for ln in flood + real: io_func(Mock(), ln)
        return 0
    with patch('mama.types.git.SubProcess.run', side_effect=fake_run), \
         patch('mama.types.git.console', side_effect=lambda t, **k: consoled.append(t)), \
         patch('mama.types.git.progress', side_effect=lambda t, **k: progressed.append(t)), \
         patch('mama.types.git.ssh_multiplex.ensure_master_for_url'), \
         patch('mama.types.git.ssh_multiplex.fetch_slot', side_effect=lambda: contextlib.nullcontext()):
        dep.dep_source.run_git(dep, 'fetch --unshallow')
    assert not any('Receiving objects' in c for c in consoled)  # no raw per-percent flood on the console
    assert len(progressed) <= 5                                 # collapsed into a few throttled redraws
    assert not any('new branch' in c or 'From http' in c for c in consoled)  # transfer bookkeeping stays quiet
    assert any('repository is shallow' in c for c in consoled)  # a real warning still reaches the user


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
