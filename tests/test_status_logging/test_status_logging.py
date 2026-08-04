"""Pins the `verbose` working-tree status log: silent by default, and one honest line per check."""
import os, subprocess
import pytest

from mama.utils import git_status as util
from testutils import strip_ansi


@pytest.fixture
def repo(tmp_path):
    d = tmp_path / 'dep'
    d.mkdir()
    for cmd in ('init -q', 'config user.email t@t', 'config user.name t'):
        subprocess.run(['git', *cmd.split()], cwd=str(d), capture_output=True)
    (d / 'lib.cpp').write_text('int f(){return 1;}\n')
    subprocess.run(['git', 'add', '-A'], cwd=str(d), capture_output=True)
    subprocess.run(['git', 'commit', '-q', '-m', 'init'], cwd=str(d), capture_output=True)
    return str(d)


@pytest.fixture(autouse=True)
def _quiet_again():
    yield
    util.log_status_checks = False
    util._git_fingerprints.clear()


def _check(repo, capsys, reason='why'):
    util.forget_git_dir_fingerprint(repo)
    util.git_dir_fingerprint(repo, reason=reason)
    return strip_ansi(capsys.readouterr().out)


def test_a_build_says_nothing_until_verbose_asks(repo, capsys):
    assert _check(repo, capsys) == ''


def test_the_line_names_the_dep_the_reason_the_source_and_the_result(repo, capsys):
    util.log_status_checks = True
    out = _check(repo, capsys, reason='did the source change')
    assert 'dep' in out and '[did the source change]' in out and 'own git status' in out
    assert '-> clean' in out and 'ms)' in out


def test_a_dirty_tree_names_which_kind_changed(repo, capsys):
    util.log_status_checks = True
    open(os.path.join(repo, 'extra.h'), 'w').write('#pragma once\n')
    assert '-> untracked' in _check(repo, capsys)
    open(os.path.join(repo, 'lib.cpp'), 'w').write('int f(){return 2;}\n')
    assert '-> tracked untracked' in _check(repo, capsys)


def test_a_second_reader_reports_the_memo_and_no_kind(repo, capsys):
    # the memo knows the answer but not which kind produced it, so it must not guess one
    util.log_status_checks = True
    _check(repo, capsys)
    util.git_dir_fingerprint(repo, reason='someone else')
    out = strip_ansi(capsys.readouterr().out)
    assert 'memo' in out and '-> clean' in out and 'tracked' not in out
