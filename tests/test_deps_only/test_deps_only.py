"""Pins dependency flattening, deps_only scoping, and the unified scheduler's deps_only behavior."""
import threading
from unittest.mock import Mock
import pytest
from testutils import FakeUnifiedDep, make_unified_config
from mama import dependency_chain as dc
from mama.dependency_chain import (get_flat_deps, get_flat_child_deps, get_deps_only_targets,
                                   find_dependency, DepsOnlyScope)


def make_dep(name, children=None):
    dep = Mock()
    dep.name = name
    dep.children = children or []
    dep.get_children = Mock(return_value=dep.children)
    dep.is_root = False
    dep.should_rebuild = False
    dep.already_loaded = False
    dep.already_executed = False
    return dep


def make_config(build=False, clean=False, update=False):
    config = Mock()
    config.build = build
    config.clean = clean
    config.update = update
    return config


def make_tree():
    """A mock dependency tree: root -> {A -> {C, D}, B -> {D}}. D is shared by A and B."""
    D = make_dep('D')
    C = make_dep('C')
    A = make_dep('A', children=[C, D])
    B = make_dep('B', children=[D])
    root = make_dep('root', children=[A, B])
    root.is_root = True
    return root, A, B, C, D


# --- get_flat_deps ---

def test_get_flat_deps_includes_root():
    root, A, B, C, D = make_tree()
    flat = get_flat_deps(root)
    assert flat[0] is root
    assert set(flat) == {root, A, B, C, D}


# --- get_flat_child_deps ---

def test_get_flat_child_deps_excludes_root():
    root, A, B, C, D = make_tree()
    children = get_flat_child_deps(root)
    assert root not in children
    assert set(children) == {A, B, C, D}


def test_get_flat_child_deps_of_subtarget():
    root, A, B, C, D = make_tree()
    children = get_flat_child_deps(A)
    assert set(children) == {C, D}
    assert A not in children
    assert B not in children
    assert root not in children


def test_get_flat_child_deps_of_leaf():
    root, A, B, C, D = make_tree()
    children = get_flat_child_deps(D)
    assert children == []


# --- dependency order ---

def test_flat_deps_preserves_linker_order():
    # parent-before-child is the Unix linker order
    root, A, B, C, D = make_tree()
    flat = get_flat_deps(root)
    assert flat.index(A) < flat.index(C)
    assert flat.index(A) < flat.index(D)
    assert flat.index(B) < flat.index(D)


def test_flat_child_deps_preserves_linker_order():
    root, A, B, C, D = make_tree()
    children = get_flat_child_deps(root)
    assert children.index(A) < children.index(C)
    assert children.index(A) < children.index(D)
    assert children.index(B) < children.index(D)


def test_flat_child_deps_subtarget_preserves_order():
    root, A, B, C, D = make_tree()
    children = get_flat_child_deps(A)
    # C and D are both direct children of A. The mamafile declaration order must survive: C before D.
    assert children == [C, D]


def test_shared_dep_appears_once_at_correct_position():
    root, A, B, C, D = make_tree()
    flat = get_flat_deps(root)
    assert flat.count(D) == 1
    assert flat.index(A) < flat.index(D)
    assert flat.index(B) < flat.index(D)


# --- deps_only: no target (existing behavior) ---

def test_deps_only_no_target_removes_root():
    root, A, B, C, D = make_tree()
    flat_deps = get_flat_deps(root)
    flat_deps.remove(root)
    flat_deps_reverse = list(reversed(flat_deps))
    assert root not in flat_deps
    assert root not in flat_deps_reverse
    assert set(flat_deps) == {A, B, C, D}


# --- deps_only: with target ---

def test_get_deps_only_targets_filters_to_subtarget_deps():
    root, A, B, C, D = make_tree()
    config = make_config(build=True)
    flat_deps, flat_deps_reverse = get_deps_only_targets(root, 'A', config)
    assert root not in flat_deps
    assert A not in flat_deps
    assert B not in flat_deps
    assert set(flat_deps) == {C, D}


def test_get_deps_only_targets_preserves_linker_order():
    root, A, B, C, D = make_tree()
    config = make_config(build=True)
    flat_deps, flat_deps_reverse = get_deps_only_targets(root, 'A', config)
    assert flat_deps == [C, D]


def test_get_deps_only_targets_reverse_is_build_order():
    root, A, B, C, D = make_tree()
    config = make_config(build=True)
    flat_deps, flat_deps_reverse = get_deps_only_targets(root, 'A', config)
    assert flat_deps_reverse == [D, C]


def test_get_deps_only_targets_marks_should_rebuild():
    root, A, B, C, D = make_tree()
    config = make_config(build=True)
    get_deps_only_targets(root, 'A', config)
    assert C.should_rebuild is True
    assert D.should_rebuild is True
    assert A.should_rebuild is False
    assert B.should_rebuild is False


def test_get_deps_only_targets_cleans_on_rebuild():
    root, A, B, C, D = make_tree()
    config = make_config(build=True, clean=True)
    get_deps_only_targets(root, 'A', config)
    C.clean.assert_called_once()
    C.create_build_dir_if_needed.assert_called_once()
    D.clean.assert_called_once()
    D.create_build_dir_if_needed.assert_called_once()
    A.clean.assert_not_called()
    B.clean.assert_not_called()


def test_get_deps_only_targets_no_clean_on_build():
    root, A, B, C, D = make_tree()
    config = make_config(build=True, clean=False)
    get_deps_only_targets(root, 'A', config)
    C.clean.assert_not_called()
    D.clean.assert_not_called()


def test_get_deps_only_targets_B_only_gets_D():
    root, A, B, C, D = make_tree()
    config = make_config(build=True)
    flat_deps, flat_deps_reverse = get_deps_only_targets(root, 'B', config)
    assert flat_deps == [D]
    assert flat_deps_reverse == [D]
    assert D.should_rebuild is True
    assert C.should_rebuild is False


# --- find_dependency ---

def test_find_dependency_case_insensitive():
    root, A, B, C, D = make_tree()
    assert find_dependency(root, 'a') is A
    assert find_dependency(root, 'ROOT') is root
    assert find_dependency(root, 'd') is D
    assert find_dependency(root, 'nonexistent') is None


# --- deps_only on the unified scheduler ---


@pytest.fixture
def unified(no_cmake_writes):
    """Run execute_unified over a fake tree and return the recorded (tag, name) events."""
    def run(child_specs, target_name=None, **config):
        cfg = make_unified_config(target=target_name or 'all', **config)
        ev, lock = [], threading.Lock()
        root = FakeUnifiedDep('root', cfg, ev, lock, child_specs=child_specs)
        root.is_root = True  # without a target name the root IS the scope root, so it must not build
        dc.execute_unified(root, DepsOnlyScope(cfg, target_name))
        return ev, cfg, root
    return run


def _named(ev, tag):
    return {n for t, n in ev if t == tag}


def test_unified_deps_only_builds_every_dep_but_not_the_root(unified):
    ev, _, _root = unified([('A', ()), ('B', [('C', ())])])
    assert _named(ev, 'load') == {'root', 'A', 'B', 'C'}  # the root still loads: it declares the tree
    assert _named(ev, 'bld') == {'A', 'B', 'C'}


def test_unified_deps_only_target_builds_only_that_targets_deps(unified):
    # root -> {A -> {C -> {E}}, B -> {D}}. deps_only A must build C and E, not A, B, D or root.
    ev, _, _root = unified([('A', [('C', [('E', ())])]), ('B', [('D', ())])], target_name='A')
    assert _named(ev, 'load') == {'root', 'A', 'B', 'C', 'D', 'E'}
    assert _named(ev, 'bld') == {'C', 'E'}


def test_unified_deps_only_promotes_a_dep_first_seen_outside_the_scope(no_cmake_writes):
    """Shared dep D sits under both A and the unrelated branch B, and B is two levels shallower, so the
    scheduler always discovers D outside A's subtree first. Reaching D again through A must give it
    build jobs, or `deps_only A` silently skips a dep that A needs."""
    cfg = make_unified_config(target='A')
    ev, lock = [], threading.Lock()
    mk = lambda name, kids: FakeUnifiedDep(name, cfg, ev, lock, shared_children=kids)
    d = FakeUnifiedDep('D', cfg, ev, lock, child_specs=[('E', ())])
    root = mk('root', [mk('B', [d]), mk('A1', [mk('A2', [mk('A', [d])])])])
    dc.execute_unified(root, DepsOnlyScope(cfg, 'A'))
    assert _named(ev, 'bld') == {'D', 'E'}  # E sits below D and inherits the promotion


def test_unified_deps_only_target_forces_a_rebuild_of_its_deps(unified):
    _, _, root = unified([('A', [('C', ())]), ('B', ())], target_name='A')
    assert {d.name for d in get_flat_deps(root) if d.should_rebuild} == {'C'}


def test_unified_deps_only_rebuild_cleans_the_targets_deps_only(unified):
    ev, _, _root = unified([('A', [('C', ())]), ('B', ())], target_name='A', clean=True)
    assert _named(ev, 'clean') == {'C'}


def test_unified_deps_only_without_a_target_never_cleans(unified):
    """No target means the normal up-to-date check decides, so an unchanged dep stays cached."""
    ev, _, _root = unified([('A', ()), ('B', ())], clean=True)
    assert _named(ev, 'clean') == set()


def test_unified_deps_only_widens_the_target_for_deploy(unified):
    _, cfg, _root = unified([('A', [('C', ())])], target_name='A')
    assert cfg.target == 'all'  # deploy/upload gate on config.target, which still named the excluded A


def test_unified_deps_only_exits_when_the_named_target_is_absent(unified):
    with pytest.raises(SystemExit):
        unified([('A', ())], target_name='NoSuchTarget')
