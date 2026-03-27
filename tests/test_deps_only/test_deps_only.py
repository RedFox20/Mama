from unittest.mock import Mock
from mama.dependency_chain import get_flat_deps, get_flat_child_deps, get_deps_only_targets, find_dependency


def make_dep(name, children=None):
    """Create a mock BuildDependency with the given name and children"""
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
    """
    Create a mock dependency tree:
        root
        ├── A
        │   ├── C
        │   └── D
        └── B
            └── D  (shared with A)
    """
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
    """get_flat_child_deps(A) should return only A's children: C and D"""
    root, A, B, C, D = make_tree()
    children = get_flat_child_deps(A)
    assert set(children) == {C, D}
    assert A not in children
    assert B not in children
    assert root not in children


def test_get_flat_child_deps_of_leaf():
    """A leaf node has no children"""
    root, A, B, C, D = make_tree()
    children = get_flat_child_deps(D)
    assert children == []


# --- dependency order ---

def test_flat_deps_preserves_linker_order():
    """Parents before children for Unix linker order"""
    root, A, B, C, D = make_tree()
    flat = get_flat_deps(root)
    # A is parent of C and D, so A must come before C and D
    assert flat.index(A) < flat.index(C)
    assert flat.index(A) < flat.index(D)
    # B is parent of D, so B must come before D
    assert flat.index(B) < flat.index(D)


def test_flat_child_deps_preserves_linker_order():
    """get_flat_child_deps must also preserve parent-before-child order"""
    root, A, B, C, D = make_tree()
    children = get_flat_child_deps(root)
    assert children.index(A) < children.index(C)
    assert children.index(A) < children.index(D)
    assert children.index(B) < children.index(D)


def test_flat_child_deps_subtarget_preserves_order():
    """get_flat_child_deps(A) must return [C, D] in parent-before-child order"""
    root, A, B, C, D = make_tree()
    children = get_flat_child_deps(A)
    # C and D are both direct children of A (no ordering between them),
    # but the order from mamafile should be preserved: C before D
    assert children == [C, D]


def test_shared_dep_appears_once_at_correct_position():
    """D is shared by A and B; it should appear once, after both parents"""
    root, A, B, C, D = make_tree()
    flat = get_flat_deps(root)
    assert flat.count(D) == 1
    assert flat.index(A) < flat.index(D)
    assert flat.index(B) < flat.index(D)


# --- deps_only: no target (existing behavior) ---

def test_deps_only_no_target_removes_root():
    """deps_only without a target removes root from flat_deps"""
    root, A, B, C, D = make_tree()
    flat_deps = get_flat_deps(root)
    flat_deps.remove(root)
    flat_deps_reverse = list(reversed(flat_deps))
    assert root not in flat_deps
    assert root not in flat_deps_reverse
    assert set(flat_deps) == {A, B, C, D}


# --- deps_only: with target ---

def test_get_deps_only_targets_filters_to_subtarget_deps():
    """deps_only targeting A returns only A's deps (C, D), excluding A itself"""
    root, A, B, C, D = make_tree()
    config = make_config(build=True)
    flat_deps, flat_deps_reverse = get_deps_only_targets(root, 'A', config)
    assert root not in flat_deps
    assert A not in flat_deps
    assert B not in flat_deps
    assert set(flat_deps) == {C, D}


def test_get_deps_only_targets_preserves_linker_order():
    """flat_deps from get_deps_only_targets must be in parent-before-child order"""
    root, A, B, C, D = make_tree()
    config = make_config(build=True)
    flat_deps, flat_deps_reverse = get_deps_only_targets(root, 'A', config)
    assert flat_deps == [C, D]


def test_get_deps_only_targets_reverse_is_build_order():
    """flat_deps_reverse is child-before-parent (build order)"""
    root, A, B, C, D = make_tree()
    config = make_config(build=True)
    flat_deps, flat_deps_reverse = get_deps_only_targets(root, 'A', config)
    assert flat_deps_reverse == [D, C]


def test_get_deps_only_targets_marks_should_rebuild():
    """With config.build=True, all target deps get should_rebuild=True"""
    root, A, B, C, D = make_tree()
    config = make_config(build=True)
    get_deps_only_targets(root, 'A', config)
    assert C.should_rebuild is True
    assert D.should_rebuild is True
    assert A.should_rebuild is False
    assert B.should_rebuild is False


def test_get_deps_only_targets_cleans_on_rebuild():
    """With config.clean=True, deps get cleaned and build dir recreated"""
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
    """With config.build=True but clean=False, deps are NOT cleaned"""
    root, A, B, C, D = make_tree()
    config = make_config(build=True, clean=False)
    get_deps_only_targets(root, 'A', config)
    C.clean.assert_not_called()
    D.clean.assert_not_called()


def test_get_deps_only_targets_B_only_gets_D():
    """deps_only targeting B should only include D"""
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
