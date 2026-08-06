"""Pins how mama reads a commit hash out of `git ls-remote` output."""
from unittest.mock import patch

import pytest

from testutils import make_git_and_mock_dep


def _resolved(output):
    git, dep = make_git_and_mock_dep(branch='master')
    with patch('mama.types.git.execute_piped', return_value=output), \
         patch('mama.types.git.ssh_multiplex'):
        return git.init_commit_hash(dep, use_cache=False, fetch_remote=True)


@pytest.mark.parametrize('output', [
    'caf5158061bd10e79c9f042abb62c86bc6f3e7a7\trefs/heads/master',
    'caf5158061bd10e79c9f042abb62c86bc6f3e7a7\trefs/heads/master\n'
    'aaaa158061bd10e79c9f042abb62c86bc6f3e7a7\trefs/heads/other',
])
def test_a_tab_separated_answer_gives_the_short_hash(output):
    # ls-remote separates the hash from the ref with a TAB. A split on a space keeps the whole line,
    # and the ref then reaches every path that names a package.
    assert _resolved(output) == 'caf5158'


def test_an_empty_answer_stays_empty():
    assert _resolved('') == ''
