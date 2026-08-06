"""Pins that the walk gate arms itself on a build where nothing changed.

That is the run the gate exists for, and it is also the run where nothing rebuilds. Recording the walk
only from `save_status`, which needs a successful build, left the gate dead: a real project answered
45 `git status` calls where it had answered 24 before.
"""
import os, time
from unittest.mock import patch
import pytest

from mama.utils import git_status as util
from testutils import git_init_commit, make_mock_dep


def _repo(dep):
    git_init_commit(dep.src_dir, files={'lib.cpp': 'int f(){return 1;}\n'})
    return dep


@pytest.fixture
def dep(tmp_path):
    return _repo(make_mock_dep(tmp_path, name='libfoo'))


def _changed(dep) -> bool:
    util.forget_git_dir_fingerprint(dep.src_dir)
    return dep.dep_source.source_tree_changed(dep)


def test_a_build_that_changes_nothing_arms_the_gate(dep):
    # no save_status, because nothing rebuilt. The check itself has to record the walk.
    with patch.object(util.System, 'windows', True):
        assert _changed(dep) is False
        assert os.path.isfile(util.source_walk_file(dep.build_dir))


def test_the_second_build_then_spawns_no_git(dep):
    with patch.object(util.System, 'windows', True):
        assert _changed(dep) is False              # first build records the walk
        with patch('mama.utils.git_status._git_output') as git_output:
            assert _changed(dep) is False
        git_output.assert_not_called()             # second build asks git nothing at all


def test_a_changed_source_never_arms_the_gate(dep):
    with patch.object(util.System, 'windows', True):
        _changed(dep)                                            # arm it on a clean tree
        time.sleep(0.01)
        open(f'{dep.src_dir}/lib.cpp', 'w').write('int f(){return 2;}\n')
        recorded = util.read_text_from(util.source_walk_file(dep.build_dir))
        assert _changed(dep) is True
        # the walk must still name the state of the LAST build, or the rebuild would be forgotten
        assert util.read_text_from(util.source_walk_file(dep.build_dir)) == recorded
