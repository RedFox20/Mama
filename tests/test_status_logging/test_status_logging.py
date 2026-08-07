"""Pins the `verbose` working-tree status log: silent by default, and one honest line per check."""
import os
import pytest

from mama.utils import git_status as util
from testutils import strip_ansi


@pytest.fixture(autouse=True)
def _quiet_again():
    yield
    util.log_status_checks = False
    util._git_fingerprints.clear()


def _check(source_repo, capsys, reason='why'):
    util.forget_git_dir_fingerprint(source_repo)
    util.git_dir_fingerprint(source_repo, reason=reason)
    return strip_ansi(capsys.readouterr().out)


def test_a_build_says_nothing_until_verbose_asks(source_repo, capsys):
    assert _check(source_repo, capsys) == ''


def test_the_line_names_the_dep_the_reason_the_source_and_the_result(source_repo, capsys):
    util.log_status_checks = True
    out = _check(source_repo, capsys, reason='did the source change')
    assert 'dep' in out and '[did the source change]' in out and 'own git status' in out
    assert '-> clean' in out and 'ms)' in out


def test_a_dirty_tree_names_which_kind_changed(source_repo, capsys):
    util.log_status_checks = True
    open(os.path.join(source_repo, 'extra.h'), 'w').write('#pragma once\n')
    assert '-> untracked' in _check(source_repo, capsys)
    open(os.path.join(source_repo, 'lib.cpp'), 'w').write('int f(){return 2;}\n')
    assert '-> tracked untracked' in _check(source_repo, capsys)


def test_a_second_reader_reports_the_memo_and_no_kind(source_repo, capsys):
    # the memo knows the answer but not which kind produced it, so it must not guess one
    util.log_status_checks = True
    _check(source_repo, capsys)
    util.git_dir_fingerprint(source_repo, reason='someone else')
    out = strip_ansi(capsys.readouterr().out)
    assert 'memo' in out and '-> clean' in out and 'tracked' not in out
