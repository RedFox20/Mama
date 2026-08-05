"""Pins the load scope of a targeted run: a no-source dep outside the target defers its
clone, and the revive pass clones only the deps the subtree of the target needs."""
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from testutils import make_mock_dep, make_tree_dep as _fake

import mama.dependency_chain as chain
from mama.dependency_chain import reload_deferred_deps, revive_deferred_target_deps
from mama.main import mamabuild
from mama.types.git import Git


def _targeted_dep(tmp_path, target='other'):
    dep = make_mock_dep(tmp_path, target=target, deps_only=False)
    dep.config.targets_all.return_value = False
    return dep


def test_a_no_source_dep_outside_the_target_defers_its_clone(tmp_path):
    dep = _targeted_dep(tmp_path)
    with patch.object(Git, 'dependency_checkout') as checkout:
        assert dep._git_checkout_if_needed() is False
    checkout.assert_not_called()
    assert dep.load_deferred


def test_the_named_target_still_clones(tmp_path):
    dep = _targeted_dep(tmp_path)
    dep.config.target_matches.return_value = True
    with patch.object(Git, 'dependency_checkout', return_value=True) as checkout:
        assert dep._git_checkout_if_needed() is True
    checkout.assert_called_once()
    assert not dep.load_deferred


def test_an_untargeted_run_never_defers(tmp_path):
    for target, matches_all in ((None, False), ('all', True)):
        dep = make_mock_dep(tmp_path / str(target), target=target, deps_only=False)
        dep.config.targets_all.return_value = matches_all
        assert not dep._defer_clone()


def test_a_real_clone_updates_instead_of_deferring(tmp_path):
    dep = _targeted_dep(tmp_path)
    os.makedirs(f'{dep.src_dir}/.git')
    assert not dep._defer_clone()


def test_guarded_source_is_not_deferred(tmp_path):
    # dependency_checkout guards real source: it prints SKIP CLONE and builds the tree as-is
    dep = _targeted_dep(tmp_path)
    os.makedirs(dep.src_dir)
    Path(dep.src_dir, 'main.cpp').write_text('int main() { return 0; }\n')
    assert not dep._defer_clone()


def test_deps_only_never_defers(tmp_path):
    dep = _targeted_dep(tmp_path)
    dep.config.deps_only = True
    assert not dep._defer_clone()


def test_revive_makes_the_next_load_clone(tmp_path):
    dep = _targeted_dep(tmp_path)
    assert dep._defer_clone()
    dep.already_loaded = True
    dep.revive_deferred_load()
    assert not dep.load_deferred and not dep.already_loaded and dep.target is None
    assert not dep._defer_clone()   # the revived load must reach dependency_checkout


def test_reload_deferred_deps_revives_the_whole_scope():
    a = _fake('A', deferred=True)
    x = _fake('X', [a])
    with patch.object(chain, 'load_dependency_chain') as load:
        assert reload_deferred_deps(x) is True
    assert a.revived and load.call_count == 1


def test_a_reload_that_discovers_a_deferred_child_loops():
    b = _fake('B', deferred=True)
    a = _fake('A', deferred=True)
    x = _fake('X', [a])
    def grow(scope):
        if b not in x.children: x.children.append(b)   # the reload of A discovered B
    with patch.object(chain, 'load_dependency_chain', side_effect=grow) as load:
        assert reload_deferred_deps(x) is True
    assert b.revived and load.call_count == 2


def test_deps_outside_the_target_subtree_stay_deferred():
    outside = _fake('outside', deferred=True)
    target = _fake('X')
    root = _fake('root', [target, outside])
    with patch.object(chain, 'load_dependency_chain') as load:
        revive_deferred_target_deps(root, SimpleNamespace(target='X'))
    assert outside.load_deferred and not load.called


def test_an_unknown_target_revives_nothing():
    outside = _fake('outside', deferred=True)
    root = _fake('root', [outside])
    revive_deferred_target_deps(root, SimpleNamespace(target='nope'))
    assert outside.load_deferred


def test_mamabuild_runs_the_revive_pass_for_a_targeted_build(tmp_path):
    (tmp_path / 'CMakeLists.txt').write_text('project(dummy)\n')
    x = _fake('X')
    with patch('mama.main.load_dependency_chain', side_effect=lambda r: setattr(r, 'children', [x])), \
         patch('mama.main.execute_task_chain'), patch('mama.main.execute_task_chain_parallel'), \
         patch('mama.main.execute_unified'), patch('mama.main.print_build_banner'), \
         patch('mama.main.revive_deferred_target_deps') as revive:
        mamabuild(['build', 'X'], source_dir=str(tmp_path))
    revive.assert_called_once()


def test_the_target_may_hide_below_a_deferred_dep(tmp_path):
    # check_config_target revives deferred deps before it declares the name unknown
    (tmp_path / 'CMakeLists.txt').write_text('project(dummy)\n')
    hidden = _fake('hidden')
    parent = _fake('parent', deferred=True)
    def uncover(scope):
        if hidden not in parent.children: parent.children.append(hidden)
    with patch('mama.main.load_dependency_chain', side_effect=lambda r: setattr(r, 'children', [parent])), \
         patch('mama.dependency_chain.load_dependency_chain', side_effect=uncover), \
         patch('mama.main.execute_task_chain'), patch('mama.main.execute_task_chain_parallel'), \
         patch('mama.main.execute_unified'), patch('mama.main.print_build_banner'):
        mamabuild(['build', 'hidden'], source_dir=str(tmp_path))
    assert parent.revived


def test_a_targeted_rebuild_skips_the_forced_artifactory_pass(tmp_path):
    dep = _targeted_dep(tmp_path)
    dep.config.rebuild = True
    dep.config.no_specific_target.return_value = False
    Path(dep.build_dir, 'mamafile_tag').write_text('tag')   # a warm dir: not a first-time build
    assert not dep.is_first_time_build()
    dep.config.no_specific_target.return_value = True       # `mama rebuild` still refreshes every dep
    assert dep.is_first_time_build()
