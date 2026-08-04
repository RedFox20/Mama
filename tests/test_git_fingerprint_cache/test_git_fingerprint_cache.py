"""Pins the git_dir_fingerprint memo: one answer per source dir, dropped when mama changes the tree."""
from unittest.mock import patch
import pytest

from mama import util


@pytest.fixture(autouse=True)
def _empty_memo():
    util._git_fingerprints.clear()
    yield
    util._git_fingerprints.clear()


def _count_calls(src_dir, times):
    """Call the fingerprint `times` times over a stub, and return how often the git work really ran."""
    with patch('mama.util._compute_git_dir_fingerprint', return_value='abc123') as compute:
        results = [util.git_dir_fingerprint(src_dir) for _ in range(times)]
    assert results == ['abc123'] * times
    return compute.call_count


def test_one_build_asks_git_once_per_source_dir(tmp_path):
    # save_status runs twice per dependency per build, and each answer costs two git processes
    assert _count_calls(str(tmp_path), 3) == 1


def test_forgetting_a_dir_makes_the_next_call_ask_again(tmp_path):
    _count_calls(str(tmp_path), 1)
    util.forget_git_dir_fingerprint(str(tmp_path))
    assert _count_calls(str(tmp_path), 1) == 1


def test_two_source_dirs_keep_their_own_answers(tmp_path):
    other = tmp_path / 'other'; other.mkdir()
    _count_calls(str(tmp_path), 1)
    assert _count_calls(str(other), 1) == 1


def test_the_memo_switches_off_for_a_test_that_edits_the_tree(tmp_path, monkeypatch):
    monkeypatch.setattr(util, 'memoize_git_fingerprints', False)
    assert _count_calls(str(tmp_path), 3) == 3


def test_a_missing_dir_answers_empty_and_stores_nothing(tmp_path):
    assert util.git_dir_fingerprint(str(tmp_path / 'gone')) == ''
    assert util._git_fingerprints == {}
