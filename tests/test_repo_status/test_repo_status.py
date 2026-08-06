"""Pins the shared `git status`: one process answers every local dep, with the same value as before."""
from pathlib import Path
from unittest.mock import patch

from testutils import make_git_root_with_local_pkgs, git_init_commit
from mama.utils import git_status as util
from mama.utils.paths import normalized_path


def _spawn_count(fn):
    """(result, git processes spawned) for one call."""
    with patch('mama.utils.git_status._git_output', wraps=util._git_output) as git:
        return fn(), git.call_count


def test_one_status_answers_every_local_dep(tmp_path):
    deps = make_git_root_with_local_pkgs(tmp_path, count=5)
    root = Path(deps[0].src_dir).parent.parent

    _, load_spawns = _spawn_count(lambda: util.load_repo_status(str(root)))
    fingerprints, lookup_spawns = _spawn_count(
        lambda: [d.dep_source.working_tree_fingerprint(d) for d in deps])

    assert load_spawns == 1  # the toplevel comes off disk, so only the status itself spawns
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


def test_the_toplevel_walk_finds_the_dir_that_holds_the_git_entry(tmp_path):
    root = tmp_path / 'root'
    deep = root / 'a' / 'b'
    deep.mkdir(parents=True)
    git_init_commit(root, files={'lib.cpp': 'int f(){return 1;}\n'})
    assert util.find_repo_toplevel(str(deep)) == normalized_path(str(root))


def test_the_toplevel_walk_accepts_a_git_file(tmp_path):
    # a worktree and a submodule both mark their root with a `.git` FILE, not a dir
    root = tmp_path / 'wt'
    root.mkdir()
    (root / '.git').write_text('gitdir: /elsewhere/.git/worktrees/wt\n')
    assert util.find_repo_toplevel(str(root)) == normalized_path(str(root))


def _clone_inside_the_workspace(tmp_path):
    """A git dep as mama really lays it out: `<root>/packages/<name>/<name>`, its own repo, and the
    whole workspace dir hidden from the root repo by .gitignore."""
    root = tmp_path / 'root'
    (root / 'src').mkdir(parents=True)
    (root / 'src' / 'main.cpp').write_text('int main(){ return 0; }\n')
    (root / '.gitignore').write_text('packages/\n')
    git_init_commit(root)
    clone = root / 'packages' / 'libgit' / 'libgit'
    clone.mkdir(parents=True)
    (clone / 'dep.cpp').write_text('int dep(){ return 1; }\n')
    git_init_commit(clone)
    return root, clone


def test_an_edited_clone_under_the_workspace_is_never_called_clean(tmp_path):
    """The wall this guards: .gitignore hides `packages/` from the root status, so that status reports
    NO change for an edited clone. Answering from it would skip a rebuild the source needs."""
    root, clone = _clone_inside_the_workspace(tmp_path)
    util.load_repo_status(str(root))
    (clone / 'dep.cpp').write_text('int dep(){ return 99; }\n')

    assert util._repo_status_kinds(str(clone)) is None  # its own repo, so the shared status refuses it
    assert util._compute_git_dir_fingerprint(str(clone), shared_status=True) != ''
    assert not [c for c in util._repo_status[1] if 'packages' in c]  # the root status really is blind to it


def test_the_root_working_tree_still_reads_the_shared_status(tmp_path):
    """The root source dir holds .git too, and it IS the repo the status covers, so it must not fall
    into the separate-repo branch above."""
    root, _ = _clone_inside_the_workspace(tmp_path)
    util.load_repo_status(str(root))
    assert util._repo_status_kinds(str(root)) == (False, False)


def test_the_memo_keeps_the_two_modes_apart(tmp_path):
    """One wrong caller must not store its answer under the key the right caller reads."""
    root, clone = _clone_inside_the_workspace(tmp_path)
    util.load_repo_status(str(root))
    (clone / 'dep.cpp').write_text('int dep(){ return 5; }\n')
    with patch('mama.utils.git_status._compute_git_dir_fingerprint', side_effect=['shared', 'asked']) as compute:
        assert util.git_dir_fingerprint(str(clone), shared_status=True) == 'shared'
        assert util.git_dir_fingerprint(str(clone), shared_status=False) == 'asked'
    assert compute.call_count == 2
    util.forget_git_dir_fingerprint(str(clone))
    assert not [k for k in util._git_fingerprints if k[0] == str(clone)]


def test_path_case_does_not_break_the_match(tmp_path):
    deps = make_git_root_with_local_pkgs(tmp_path, count=1)
    root = Path(deps[0].src_dir).parent.parent
    util.load_repo_status(str(root))
    swapped = str(deps[0].src_dir).swapcase()
    expected = (False, False) if util.System.windows else None
    assert util._repo_status_kinds(swapped) == expected


def test_a_dir_that_git_does_not_track_loads_no_status(tmp_path):
    """Patches the walk rather than using a bare dir: pytest puts tmp_path inside this repo, so the walk
    finds a real toplevel above it and the dir is tracked after all."""
    plain = tmp_path / 'plain'
    plain.mkdir()
    with patch('mama.utils.git_status.find_repo_toplevel', return_value=''), \
         patch('mama.utils.git_status._git_output') as git:
        util.load_repo_status(str(plain))
        git.assert_not_called()  # no toplevel means no status to ask for
    assert util._repo_status_kinds(str(plain)) is None


def test_a_rename_entry_does_not_shift_the_parse():
    """A rename stores the old path in a field of its own, and that field carries no status code."""
    payload = b'R  libs/new.cpp\0libs/old.cpp\0 M libs/other.cpp\0?? libs/extra.h\0'
    assert util._parse_status(payload) == {'libs/new.cpp': 'R ', 'libs/other.cpp': ' M',
                                           'libs/extra.h': '??'}
