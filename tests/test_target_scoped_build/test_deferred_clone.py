"""Pins the two load stages of a targeted run: stage one explores the graph for free, and stage two
loads only the deps the subtree of the target needs."""
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from testutils import make_mock_dep, make_mock_shim_dep, make_tree_dep as _fake, stub_loaders

import mama.build_dependency as build_dependency
import mama.dependency_chain as chain
from mama.dependency_chain import reload_deferred_deps, revive_deferred_target_deps
from mama.main import mamabuild
from mama.types.git import Git


def _targeted_dep(tmp_path, target='other', **over):
    dep = make_mock_dep(tmp_path, target=target, deps_only=False, **over)
    dep.config.targets_all.return_value = False
    return dep


def _targeted_shim_dep(tmp_path, **over):
    dep = make_mock_shim_dep(tmp_path, target='other', deps_only=False, **over)
    dep.config.targets_all.return_value = False
    return dep


def test_a_no_source_dep_outside_the_target_defers_its_load(tmp_path):
    assert _targeted_dep(tmp_path)._defer_load()


def test_the_named_target_still_loads(tmp_path):
    dep = _targeted_dep(tmp_path)
    dep.config.target_matches.return_value = True
    assert not dep._defer_load()


def test_an_untargeted_run_never_defers(tmp_path):
    for target, matches_all in ((None, False), ('all', True)):
        dep = make_mock_dep(tmp_path / str(target), target=target, deps_only=False)
        dep.config.targets_all.return_value = matches_all
        assert not dep._defer_load()


def test_a_real_clone_is_free_so_it_loads(tmp_path):
    dep = _targeted_dep(tmp_path)
    os.makedirs(f'{dep.src_dir}/.git')
    assert not dep._defer_load()


def test_local_source_is_free_so_it_loads(tmp_path):
    dep = _targeted_dep(tmp_path)
    os.makedirs(dep.src_dir)
    Path(dep.src_dir, 'main.cpp').write_text('int main() { return 0; }\n')
    assert not dep._defer_load()


def test_a_cached_shim_defers_but_stays_free_to_expand(tmp_path):
    dep = _targeted_shim_dep(tmp_path)
    assert dep._defer_load() and dep.load_is_free()


@pytest.mark.parametrize('flag', ['update', 'disable_artifactory'])
def test_a_cached_shim_that_re_probes_the_remote_is_not_free(tmp_path, flag):
    assert not _targeted_shim_dep(tmp_path, **{flag: True}).load_is_free()


def test_deps_only_never_defers(tmp_path):
    dep = _targeted_dep(tmp_path)
    dep.config.deps_only = True
    assert not dep._defer_load()


def test_a_deferred_load_touches_no_network(tmp_path):
    # locating one target must never cost a fetch for every git dep in the graph
    dep = _targeted_dep(tmp_path, build=True)
    with patch.object(build_dependency, 'try_load_artifactory_shim') as shim, \
         patch.object(build_dependency, 'artifactory_fetch_and_reconfigure') as fetch, \
         patch.object(Git, 'dependency_checkout') as checkout:
        dep._load()
    shim.assert_not_called()
    fetch.assert_not_called()
    checkout.assert_not_called()
    assert dep.load_deferred and dep.target is not None   # the dep still holds a name for find_dependency


def test_revive_makes_the_next_load_fetch(tmp_path):
    dep = _targeted_dep(tmp_path)
    assert dep._defer_load()
    dep.already_loaded = True
    dep.revive_deferred_load()
    assert not dep.load_deferred and not dep.already_loaded and dep.target is None
    assert not dep._defer_load()   # the revived load must reach the shim probe and the checkout


def _walkable_root(tmp_path, target):
    """A root whose load reveals a local branch holding `deep`, plus a git branch and a local leaf."""
    root = make_mock_dep(tmp_path, name='root', target=target, deps_only=False)
    root.config.targets_all.return_value = False
    root.config.target_matches = lambda name, t=target: name.lower() == t.lower()
    skimmed = []
    def branch(name, children=(), is_src=True):
        d = _fake(name, children)
        d.dep_source = SimpleNamespace(is_src=is_src)
        d.is_real_clone = lambda: False
        d.skim = lambda d=d: skimmed.append(d.name)
        return d
    root.children = [branch('gitdep', is_src=False), branch('local', [branch('deep')]), branch('sibling')]
    return root, skimmed


def test_the_walk_stops_as_soon_as_the_graph_names_the_target(tmp_path):
    root, skimmed = _walkable_root(tmp_path, target='local')
    with patch.object(build_dependency.BuildDependency, 'load'):
        chain.load_path_to_target(root)
    assert skimmed == []   # the root load already named `local`, so no child skim ran


def test_the_walk_reads_a_local_branch_before_a_git_branch(tmp_path):
    root, skimmed = _walkable_root(tmp_path, target='deep')
    with patch.object(build_dependency.BuildDependency, 'load'):
        chain.load_path_to_target(root)
    assert skimmed == ['local']   # the local branch named `deep`, so gitdep and sibling stayed unread


def test_the_walk_never_turns_a_cached_shim_into_a_clone(tmp_path):
    # the walk must never replace a cached package with a git clone
    dep = _targeted_shim_dep(tmp_path)
    with patch.object(Git, 'dependency_checkout') as checkout, \
         patch.object(build_dependency, 'try_load_artifactory_shim') as probe:
        dep._load()
    checkout.assert_not_called()
    probe.assert_not_called()
    assert dep.is_artifactory_shim() and not dep.is_real_clone()


def test_reload_deferred_deps_revives_the_whole_scope():
    a = _fake('A', deferred=True)
    x = _fake('X', [a])
    with patch.object(chain, 'load_dependency_chain') as load:
        assert reload_deferred_deps(x) is True
    assert a.revived and load.call_count == 1


def test_a_free_only_reload_leaves_the_deps_that_need_the_network():
    cached, fetch = _fake('cached', deferred=True, free=True), _fake('fetch', deferred=True)
    x = _fake('X', [cached, fetch])
    with patch.object(chain, 'load_dependency_chain'):
        assert reload_deferred_deps(x, free_only=True) is True
    assert cached.revived and not fetch.revived   # the fetch waits until the name is still missing


def test_a_reload_that_discovers_a_deferred_child_loops():
    b = _fake('B', deferred=True)
    a = _fake('A', deferred=True)
    x = _fake('X', [a])
    def grow(scope, display=None):
        if b not in x.children: x.children.append(b)   # the reload of A discovered B
    with patch.object(chain, 'load_dependency_chain', side_effect=grow) as load:
        assert reload_deferred_deps(x) is True
    assert b.revived and load.call_count == 2


def test_deps_outside_the_target_subtree_stay_deferred():
    outside = _fake('outside', deferred=True)
    inside = _fake('inside', deferred=True)
    root = _fake('root', [_fake('X', [inside]), outside])
    with patch.object(chain, 'load_dependency_chain'):
        revive_deferred_target_deps(root, SimpleNamespace(target='X'))
    assert inside.revived and outside.load_deferred


def test_an_unknown_target_revives_nothing():
    outside = _fake('outside', deferred=True)
    root = _fake('root', [outside])
    revive_deferred_target_deps(root, SimpleNamespace(target='nope'))
    assert outside.load_deferred


def test_mamabuild_runs_the_revive_pass_for_a_targeted_build(tmp_path):
    (tmp_path / 'CMakeLists.txt').write_text('project(dummy)\n')
    x = _fake('X')
    with stub_loaders(lambda r: setattr(r, 'children', [x])), \
         patch('mama.main.execute_task_chain'), patch('mama.main.execute_task_chain_parallel'), \
         patch('mama.main.execute_unified'), patch('mama.main.print_build_banner'), \
         patch('mama.main.revive_deferred_target_deps') as revive:
        mamabuild(['build', 'X'], source_dir=str(tmp_path))
    revive.assert_called_once()


def test_a_clean_loads_its_target_before_it_returns(tmp_path):
    # a clean acts inside the load of the target, so stage two must run before the clean_only return
    (tmp_path / 'CMakeLists.txt').write_text('project(dummy)\n')
    x = _fake('X')
    with stub_loaders(lambda r: setattr(r, 'children', [x])), \
         patch('mama.main.execute_task_chain'), patch('mama.main.execute_task_chain_parallel'), \
         patch('mama.main.execute_unified'), patch('mama.main.print_build_banner'), \
         patch('mama.main.revive_deferred_target_deps') as revive:
        mamabuild(['clean', 'X'], source_dir=str(tmp_path))
    revive.assert_called_once()


def test_the_target_may_hide_below_a_deferred_dep(tmp_path):
    # check_config_target revives deferred deps before it declares the name unknown
    (tmp_path / 'CMakeLists.txt').write_text('project(dummy)\n')
    hidden = _fake('hidden')
    parent = _fake('parent', deferred=True)
    def uncover(scope, display=None):
        if hidden not in parent.children: parent.children.append(hidden)
    with patch('mama.main.load_path_to_target', side_effect=lambda r: setattr(r, 'children', [parent])), \
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
