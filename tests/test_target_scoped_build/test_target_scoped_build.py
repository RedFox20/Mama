"""Pins `build target=X`: revive the unbuilt deps X needs, and ONLY those - never the whole workspace."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from mama.main import mamabuild
from testutils import make_mock_dep
from mama.dependency_chain import mark_unbuilt_target_deps


def _dep(name, children=(), usable=True):
    d = SimpleNamespace(name=name, should_rebuild=False, children=list(children))
    d.get_children = lambda d=d: d.children
    d.has_usable_artifacts = lambda usable=usable: usable
    return d


def _tree():
    #  root -> X -> {A(unbuilt), B(built)}, C(unbuilt, unrelated to X)
    a, b, c = _dep('A', usable=False), _dep('B'), _dep('C', usable=False)
    x = _dep('X', [a, b])
    return _dep('root', [x, c]), x, a, b, c


def _mark(root, target='X'):
    mark_unbuilt_target_deps(root, SimpleNamespace(target=target, print=False))


def test_an_unbuilt_dep_of_the_target_is_revived():
    root, _, a, _, _ = _tree()
    _mark(root)
    assert a.should_rebuild      # else X compiles against an include dir that does not exist


def test_an_unbuilt_dep_outside_the_target_subtree_is_left_alone():
    # marking every unbuilt dep in the workspace builds unrelated targets and lets a shelling-out mamafile fork itself
    root, _, _, _, c = _tree()
    _mark(root)
    assert not c.should_rebuild


def test_a_dep_that_depends_on_the_target_is_never_revived():
    # rpclib.configure() shells out to `mama build target=protobuf`. Reviving rpclib (ABOVE protobuf) re-enters it, forever.
    protobuf = _dep('protobuf', usable=False)
    rpc = _dep('rpclib', [protobuf], usable=False)
    root = _dep('myapp', [rpc], usable=False)
    _mark(root, target='protobuf')
    assert not rpc.should_rebuild and not root.should_rebuild   # only protobuf's own subtree is in scope


def test_a_dep_with_artifacts_is_not_rebuilt():
    root, _, _, b, _ = _tree()
    _mark(root)
    assert not b.should_rebuild  # `target=X` still means build only X


def test_an_unknown_target_marks_nothing():
    root, _, a, _, c = _tree()
    _mark(root, target='nope')
    assert not a.should_rebuild and not c.should_rebuild


def _artifact_dep(tmp_path, **over):
    dep = make_mock_dep(tmp_path)
    dep.target = Mock(build_products=[], args='')
    dep.nothing_to_build = False
    dep.from_artifactory = False
    for k, v in over.items(): setattr(dep, k, v)
    return dep


def test_has_usable_artifacts_reads_every_shape(tmp_path):
    dep = _artifact_dep(tmp_path)
    assert not dep.has_usable_artifacts()                       # empty build dir: nothing to link against
    (Path(dep.build_dir) / 'CMakeCache.txt').write_text('')
    assert dep.has_usable_artifacts()                           # configured -> built

    lib = tmp_path / 'libfoo.a'; lib.write_text('')
    custom = _artifact_dep(tmp_path)
    custom.target.build_products = [str(lib)]
    assert custom.has_usable_artifacts()                        # custom build(): exports, but no CMakeCache
    custom.target.build_products = [str(tmp_path / 'gone.a')]
    assert not custom.has_usable_artifacts()                    # a recorded export vanished

    assert _artifact_dep(tmp_path, from_artifactory=True).has_usable_artifacts()
    assert _artifact_dep(tmp_path, nothing_to_build=True).has_usable_artifacts()


def _fake_tree_root():
    """myapp -> rpcservice -> rpclib -> protobuf, plus an unrelated leaf."""
    protobuf = _dep('protobuf')
    rpc = _dep('rpclib', [protobuf])
    svc = _dep('rpcservice', [rpc])
    return _dep('myapp', [svc, _dep('gcsmanual')]), protobuf


def _executed_deps(args, tmp_path):
    """Names handed to the task chain by `mamabuild`, with loading and execution stubbed out."""
    (tmp_path / 'CMakeLists.txt').write_text('project(dummy)\n')
    root_children, _ = _fake_tree_root()
    seen = {}
    def capture(deps): seen['names'] = [d.name for d in deps]
    with patch('mama.main.load_dependency_chain', side_effect=lambda r: setattr(r, 'children', root_children.children)), \
         patch('mama.main.execute_task_chain', side_effect=capture), \
         patch('mama.main.execute_task_chain_parallel', side_effect=capture), \
         patch('mama.main.execute_unified'), \
         patch('mama.main.print_build_banner'):
        mamabuild(args, source_dir=str(tmp_path))
    return seen.get('names', [])


def test_building_one_target_does_not_execute_unrelated_targets(tmp_path):
    # an out-of-scope dep does no build work but would still run package(), which asserts on libs that were never built
    names = _executed_deps(['build', 'protobuf'], tmp_path)
    assert names == ['protobuf']
    assert 'rpcservice' not in names and 'myapp' not in names


def test_building_a_mid_tree_target_still_includes_what_it_needs(tmp_path):
    names = _executed_deps(['build', 'rpclib'], tmp_path)
    assert set(names) == {'rpclib', 'protobuf'}   # its own dep comes along, its dependents do not


def test_uploading_one_target_does_not_execute_unrelated_targets(tmp_path):
    # upload must scope like a targeted build, or an unrelated dep packages libs that were never built
    names = _executed_deps(['upload', 'protobuf'], tmp_path)
    assert names == ['protobuf']
    assert 'rpcservice' not in names and 'myapp' not in names


def test_deploying_a_mid_tree_target_scopes_to_its_subtree(tmp_path):
    names = _executed_deps(['deploy', 'rpclib'], tmp_path)
    assert set(names) == {'rpclib', 'protobuf'}   # deps come along to be packaged, dependents do not


def test_an_untargeted_build_still_runs_the_whole_tree(tmp_path):
    names = _executed_deps(['build', 'all', 'serial'], tmp_path)  # serial keeps it on the classic path
    assert {'rpcservice', 'rpclib', 'protobuf', 'gcsmanual'} <= set(names)
    assert len(names) == 5   # ...plus the root itself, named after the project dir


def _runner_used(args, tmp_path):
    """Which task runner mamabuild picked - the parallel one owns the live display."""
    (tmp_path / 'CMakeLists.txt').write_text('project(dummy)\n')
    root_children, _ = _fake_tree_root()
    with patch('mama.main.load_dependency_chain', side_effect=lambda r: setattr(r, 'children', root_children.children)), \
         patch('mama.main.execute_task_chain') as serial, \
         patch('mama.main.execute_task_chain_parallel') as parallel, \
         patch('mama.main.execute_unified'), \
         patch('mama.main.print_build_banner'):
        mamabuild(args, source_dir=str(tmp_path))
    return 'serial' if serial.called else ('parallel' if parallel.called else 'none')


def test_a_single_target_build_still_uses_the_live_display(tmp_path):
    # The scheduler owns the display and the serial runner dumps raw cmake output: a one-dep graph must not fall back to it.
    assert _runner_used(['build', 'protobuf'], tmp_path) == 'parallel'


def test_serial_flag_still_opts_out(tmp_path):
    assert _runner_used(['build', 'protobuf', 'serial'], tmp_path) == 'serial'


def test_mamabuild_actually_revives_an_unbuilt_dep(tmp_path):
    # the unit tests above call mark_unbuilt_target_deps directly, so only driving mamabuild proves the wiring exists
    (tmp_path / 'CMakeLists.txt').write_text('project(dummy)\n')
    protobuf = _dep('protobuf', usable=False)
    rpc = _dep('rpclib', [protobuf])
    with patch('mama.main.load_dependency_chain', side_effect=lambda r: setattr(r, 'children', [rpc])), \
         patch('mama.main.execute_task_chain'), patch('mama.main.execute_task_chain_parallel'), \
         patch('mama.main.execute_unified'), \
         patch('mama.main.print_build_banner'):
        mamabuild(['build', 'rpclib'], source_dir=str(tmp_path))
    assert protobuf.should_rebuild     # else rpclib compiles against an include dir that is not there


def test_has_usable_artifacts_survives_a_dep_whose_target_never_loaded(tmp_path):
    # a dep can sit in the tree with target=None (mamafile failed to parse), and the scoping pass walks every child
    from pathlib import Path
    dep = _artifact_dep(tmp_path)
    dep.target = None
    assert dep.has_usable_artifacts() is False
    (Path(dep.build_dir) / 'CMakeCache.txt').write_text('')
    assert dep.has_usable_artifacts() is True   # judged by the build dir alone
