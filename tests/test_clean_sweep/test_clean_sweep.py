"""Pins `clean all`'s disk sweep: every unreachable build dir of THIS config goes, including a dep's
args dirs. Another config's dir or a non-mama dir is never touched."""
from types import SimpleNamespace
import pytest
from mama.build_names import is_build_dir_of
from mama.dependency_chain import sweep_orphaned_build_dirs


def _marked(ws, name, build_dir, marker='CMakeCache.txt'):
    d = ws / name / build_dir; d.mkdir(parents=True, exist_ok=True); (d / marker).write_text('')


def _workspace(tmp_path, build_dir_name='linux'):
    ws = tmp_path / 'packages'
    for name, files in (('protobuf', ['CMakeCache.txt']), ('SDL', ['mama_shim']), ('zlib', ['mama_exported_libs'])):
        d = ws / name / 'linux'; d.mkdir(parents=True)
        for f in files: (d / f).write_text('')
    (ws / 'protobuf' / 'protobuf').mkdir()                      # a source tree, not a build dir
    (ws / 'notmine' / 'linux').mkdir(parents=True)              # a dir with no mama marker
    (ws / 'protobuf' / 'windows').mkdir()                       # another platform: out of scope
    (ws / 'protobuf' / 'windows' / 'CMakeCache.txt').write_text('')
    root = SimpleNamespace(dep_dir=str(ws / 'root'), build_dir_name=build_dir_name)
    return ws, root, SimpleNamespace(print=False)


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
    config = SimpleNamespace(print=False)
    root = SimpleNamespace(dep_dir=str(tmp_path / 'gone' / 'x'), build_dir_name='linux')
    assert sweep_orphaned_build_dirs(root, config) == 0


def test_sweep_reaches_the_args_dirs_of_one_dep(tmp_path):
    # An unreachable dep declares no args, so its linux-lgpl dir used to survive every clean.
    ws, root, config = _workspace(tmp_path)
    _marked(ws, 'libffmpeg', 'linux-lgpl')
    _marked(ws, 'libffmpeg', 'linux-cpp20-lgpl')
    assert sweep_orphaned_build_dirs(root, config) == 5
    assert not (ws / 'libffmpeg' / 'linux-lgpl').exists()
    assert not (ws / 'libffmpeg' / 'linux-cpp20-lgpl').exists()


@pytest.mark.parametrize('other', ['linux-asan', 'linux-cov', 'linux-clang', 'linux-clang-cov-asan'])
def test_sweep_never_touches_another_configs_dir(tmp_path, other):
    # `mama linux clean all` cleans one config. A sanitizer, coverage or clang build is a different one.
    ws, root, config = _workspace(tmp_path)
    _marked(ws, 'protobuf', other)
    assert sweep_orphaned_build_dirs(root, config) == 3
    assert (ws / 'protobuf' / other).exists()


def test_a_sanitizer_config_sweeps_its_own_args_dirs_only(tmp_path):
    ws, root, config = _workspace(tmp_path, build_dir_name='linux-asan')
    _marked(ws, 'libffmpeg', 'linux-asan')
    _marked(ws, 'libffmpeg', 'linux-asan-lgpl')
    _marked(ws, 'libffmpeg', 'linux-asan-ubsan')  # asan+ubsan is another config, not an arg
    _marked(ws, 'libffmpeg', 'linux')             # the plain config: not ours either
    assert sweep_orphaned_build_dirs(root, config) == 2
    assert not (ws / 'libffmpeg' / 'linux-asan').exists()
    assert not (ws / 'libffmpeg' / 'linux-asan-lgpl').exists()
    assert (ws / 'libffmpeg' / 'linux-asan-ubsan').exists() and (ws / 'libffmpeg' / 'linux').exists()


@pytest.mark.parametrize('dir_name, config_dir, mine', [
    ('linux',            'linux',      True),
    ('linux-lgpl',       'linux',      True),
    ('linux-cpp20-lgpl', 'linux',      True),
    ('linux-asan',       'linux',      False),  # a config token: another config
    ('linux-cov',        'linux',      False),
    ('linux-clang',      'linux',      False),
    ('linux32',          'linux',      False),  # another arch, and no '-' boundary
    ('linux-asan-lgpl',  'linux-asan', True),
    ('linux-asan-ubsan', 'linux-asan', False),
    ('linux',            'linux-asan', False),
])
def test_which_dirs_belong_to_a_config(dir_name, config_dir, mine):
    assert is_build_dir_of(dir_name, config_dir) is mine
