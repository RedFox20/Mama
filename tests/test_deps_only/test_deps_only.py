"""Pins dependency flattening, deps_only scoping, and the unified scheduler's deps_only behavior."""
import threading, time
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


# --- flattening: who is in the list, and in what order ---

def test_get_flat_deps_leads_with_the_root_and_names_every_dep_once():
    # parent-before-child is the Unix linker order, and a shared dep appears once
    root, A, B, C, D = make_tree()
    flat = get_flat_deps(root)
    assert flat[0] is root and set(flat) == {root, A, B, C, D}
    assert flat.count(D) == 1
    assert flat.index(A) < flat.index(C) < flat.index(D) and flat.index(B) < flat.index(D)


def test_get_flat_child_deps_drops_the_root_and_keeps_the_order():
    root, A, B, C, D = make_tree()
    children = get_flat_child_deps(root)
    assert set(children) == {A, B, C, D}
    assert children.index(A) < children.index(C) and children.index(B) < children.index(D)


def test_get_flat_child_deps_of_a_subtarget_takes_its_subtree_alone():
    # C and D are both direct children of A. The mamafile declaration order must survive: C before D.
    root, A, B, C, D = make_tree()
    assert get_flat_child_deps(A) == [C, D]
    assert get_flat_child_deps(D) == []


# --- deps_only: with target ---

def test_get_deps_only_targets_takes_the_subtree_of_the_target_alone():
    root, A, B, C, D = make_tree()
    flat_deps, flat_deps_reverse = get_deps_only_targets(root, 'A', make_config(build=True))
    assert flat_deps == [C, D]                   # the target itself, its parents and its siblings stay out
    assert flat_deps_reverse == [D, C]           # leaves first, which is the build order
    assert (C.should_rebuild, D.should_rebuild) == (True, True)
    assert (A.should_rebuild, B.should_rebuild) == (False, False)


def test_get_deps_only_targets_of_a_second_parent_takes_the_shared_dep_alone():
    root, A, B, C, D = make_tree()
    flat_deps, flat_deps_reverse = get_deps_only_targets(root, 'B', make_config(build=True))
    assert flat_deps == flat_deps_reverse == [D]
    assert D.should_rebuild is True and C.should_rebuild is False


@pytest.mark.parametrize('clean', [True, False])
def test_get_deps_only_targets_cleans_the_deps_of_the_target_only_on_a_rebuild(clean):
    root, A, B, C, D = make_tree()
    get_deps_only_targets(root, 'A', make_config(build=True, clean=clean))
    for dep in (C, D):
        assert dep.clean.called is clean
        assert dep.create_build_dir_if_needed.called is clean
    A.clean.assert_not_called()
    B.clean.assert_not_called()


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


def test_unified_deps_only_promotes_a_dep_whose_child_has_no_job_yet(no_cmake_writes):
    """A dep names its children before it grows the graph, so a promotion can reach a child that no job
    knows yet. D holds inside that window until A promotes it."""
    cfg = make_unified_config(target='A')
    ev, lock = [], threading.Lock()
    scope = DepsOnlyScope(cfg, 'A')
    named_children = threading.Event()

    class HoldsItsGrowth(FakeUnifiedDep):
        def load(self):
            super().load()
            named_children.set()
            deadline = time.monotonic() + 5
            while not scope.is_inside(self) and time.monotonic() < deadline: time.sleep(0.001)

    class WaitsForD(FakeUnifiedDep):
        def load(self):
            named_children.wait(5)
            super().load()

    mk = lambda name, kids: FakeUnifiedDep(name, cfg, ev, lock, shared_children=kids)
    d = HoldsItsGrowth('D', cfg, ev, lock, child_specs=[('E', ())])
    root = mk('root', [mk('B', [d]), WaitsForD('A', cfg, ev, lock, shared_children=[d])])
    dc.execute_unified(root, scope)
    assert _named(ev, 'bld') == {'D', 'E'}


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
