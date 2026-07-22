"""Pins `clean all`'s disk sweep: build dirs the tree walk can't reach are still cleaned, non-mama dirs aren't."""
from types import SimpleNamespace
from mama.dependency_chain import sweep_orphaned_build_dirs


def _workspace(tmp_path):
    ws = tmp_path / 'packages'
    for name, files in (('protobuf', ['CMakeCache.txt']), ('SDL', ['mama_shim']), ('zlib', ['mama_exported_libs'])):
        d = ws / name / 'linux'; d.mkdir(parents=True)
        for f in files: (d / f).write_text('')
    (ws / 'protobuf' / 'protobuf').mkdir()                      # a source tree, not a build dir
    (ws / 'notmine' / 'linux').mkdir(parents=True)              # a dir with no mama marker
    (ws / 'protobuf' / 'windows').mkdir()                       # another platform: out of scope
    (ws / 'protobuf' / 'windows' / 'CMakeCache.txt').write_text('')
    root = SimpleNamespace(dep_dir=str(ws / 'root'))
    return ws, root, SimpleNamespace(print=False, platform_build_dir_name=lambda: 'linux')


def test_sweep_removes_marked_build_dirs_for_this_platform(tmp_path):
    ws, root, config = _workspace(tmp_path)
    assert sweep_orphaned_build_dirs(root, config) == 3
    for name in ('protobuf', 'SDL', 'zlib'): assert not (ws / name / 'linux').exists()


def test_sweep_never_touches_unmarked_or_other_platform_dirs(tmp_path):
    ws, root, config = _workspace(tmp_path)
    sweep_orphaned_build_dirs(root, config)
    assert (ws / 'notmine' / 'linux').exists()        # no mama marker: not ours to delete
    assert (ws / 'protobuf' / 'protobuf').exists()    # source tree survives
    assert (ws / 'protobuf' / 'windows').exists()     # a different platform's build dir survives


def test_sweep_on_a_missing_workspace_is_a_noop(tmp_path):
    config = SimpleNamespace(print=False, platform_build_dir_name=lambda: 'linux')
    assert sweep_orphaned_build_dirs(SimpleNamespace(dep_dir=str(tmp_path / 'gone' / 'x')), config) == 0
