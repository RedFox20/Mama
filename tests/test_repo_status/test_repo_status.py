"""Pins the shared `git status`: one process answers every local dep, with the same value as before."""
from pathlib import Path
from unittest.mock import patch

from testutils import make_git_root_with_local_pkgs, make_mock_local_dep
from mama import util


def _spawn_count(fn):
    """(result, git processes spawned) for one call."""
    with patch('mama.util._git_output', wraps=util._git_output) as git:
        return fn(), git.call_count


def test_one_status_answers_every_local_dep(tmp_path):
    deps = make_git_root_with_local_pkgs(tmp_path, count=5)
    root = Path(deps[0].src_dir).parent.parent

    _, load_spawns = _spawn_count(lambda: util.load_repo_status(str(root)))
    fingerprints, lookup_spawns = _spawn_count(
        lambda: [d.dep_source.working_tree_fingerprint(d) for d in deps])

    assert load_spawns == 2  # rev-parse --show-toplevel, then one status
    assert lookup_spawns == 0  # five clean deps, and not one of them asked git
    assert fingerprints == [''] * 5


def test_shared_status_gives_the_same_answer_as_asking_git(tmp_path):
    deps = make_git_root_with_local_pkgs(tmp_path, count=3)
    root = Path(deps[0].src_dir).parent.parent
    (Path(deps[0].src_dir) / 'lib.cpp').write_text('int f0(){ return 99; }\n')  # tracked edit
    (Path(deps[1].src_dir) / 'extra.h').write_text('#pragma once\n')            # untracked file
    util.load_repo_status(str(root))                                            # deps[2] stays clean

    for dep in deps:
        shared = util._compute_git_dir_fingerprint(dep.src_dir, shared_status=True)
        asked = util._compute_git_dir_fingerprint(dep.src_dir, shared_status=False)
        assert shared == asked, dep.name
    assert util._compute_git_dir_fingerprint(deps[2].src_dir, shared_status=True) == ''


def test_a_dep_outside_the_loaded_repo_asks_git_itself(tmp_path):
    deps = make_git_root_with_local_pkgs(tmp_path, count=1)
    util.load_repo_status(str(Path(deps[0].src_dir).parent.parent))
    outside = tmp_path / 'elsewhere'
    outside.mkdir()
    assert util._repo_status_kinds(str(outside)) is None


def test_a_git_dep_never_reads_the_shared_status(tmp_path):
    """A git dep clones into the workspace dir, which .gitignore hides from the root status. Reading
    that status would report every edited clone as clean."""
    deps = make_git_root_with_local_pkgs(tmp_path, count=1)
    root = Path(deps[0].src_dir).parent.parent
    util.load_repo_status(str(root))
    _, spawns = _spawn_count(lambda: util._compute_git_dir_fingerprint(deps[0].src_dir, shared_status=False))
    assert spawns >= 1


def test_path_case_does_not_break_the_match(tmp_path):
    deps = make_git_root_with_local_pkgs(tmp_path, count=1)
    root = Path(deps[0].src_dir).parent.parent
    util.load_repo_status(str(root))
    swapped = str(deps[0].src_dir).swapcase()
    expected = (False, False) if util.System.windows else None
    assert util._repo_status_kinds(swapped) == expected


def test_a_dir_that_git_does_not_track_loads_no_status(tmp_path):
    """Patches git rather than using a bare dir: pytest puts tmp_path inside this repo, so git finds a
    real toplevel above it and the dir is tracked after all."""
    plain = tmp_path / 'plain'
    plain.mkdir()
    with patch('mama.util._git_output', return_value=b'') as git:
        util.load_repo_status(str(plain))
        assert git.call_count == 1  # rev-parse answered nothing, so no status ran
    assert util._repo_status_kinds(str(plain)) is None


def test_a_rename_entry_does_not_shift_the_parse():
    """A rename stores the old path in a field of its own, and that field carries no status code."""
    payload = b'R  libs/new.cpp\0libs/old.cpp\0 M libs/other.cpp\0?? libs/extra.h\0'
    assert util._parse_status(payload) == {'libs/new.cpp': 'R ', 'libs/other.cpp': ' M',
                                           'libs/extra.h': '??'}
