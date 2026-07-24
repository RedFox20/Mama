"""Pins build_host_binary: obtain a HOST-built tool (e.g. protoc) while cross-compiling by cheap-checking the
host build dir, then bootstrapping via a `mama <host> build` child on a miss - plus host_platform_name/host_build_dir."""
import os, sys, pytest
from unittest.mock import patch

from testutils import make_configured_target
from mama import build_config as bc
from mama import build_target as bt


def _cross_target(tmp_path, name='android', host='linux'):
    """A target cross-compiling for `name` with host `host`, so host_build_dir() is a distinct sibling."""
    t, dep = make_configured_target(tmp_path)
    dep.build_dir = os.path.join(str(tmp_path), 'packages', 'libfoo', name)
    dep.config.name.return_value = name
    dep.config.host_platform_name.return_value = host
    dep.config.root_source_dir = str(tmp_path)
    return t, dep


def _touch(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, 'w').close()
    return path


# ── host_platform_name ───────────────────────────────────────────────────────

@pytest.mark.parametrize('windows,macos,expected', [
    (True, False, 'windows'), (False, True, 'macos'), (False, False, 'linux'),
])
def test_host_platform_name_follows_the_host_os(windows, macos, expected):
    cfg = bc.BuildConfig.__new__(bc.BuildConfig)  # bypass __init__: the method reads only System
    with patch.object(bc.System, 'windows', windows), patch.object(bc.System, 'macos', macos):
        assert cfg.host_platform_name() == expected


# ── host_build_dir ───────────────────────────────────────────────────────────

def test_host_build_dir_is_a_sibling_named_after_the_host(tmp_path):
    t, dep = _cross_target(tmp_path)  # build_dir=.../libfoo/android, host=linux
    assert t.host_build_dir() == os.path.dirname(dep.build_dir).replace('\\', '/') + '/linux'
    assert t.host_build_dir('bin/protoc').endswith('/libfoo/linux/bin/protoc')


# ── build_host_binary ────────────────────────────────────────────────────────

def test_native_build_returns_the_local_binary_without_a_child(tmp_path):
    t, dep = make_configured_target(tmp_path)  # build_dir ends in 'linux'
    dep.config.name.return_value = 'linux'
    dep.config.host_platform_name.return_value = 'linux'
    binary = _touch(t.build_dir('bin/protoc'))
    with patch('mama.build_target.SubProcess.run') as run:
        assert t.build_host_binary('bin/protoc') == binary
        run.assert_not_called()


def test_cross_hit_returns_the_host_binary_without_a_child(tmp_path):
    t, dep = _cross_target(tmp_path)
    binary = _touch(t.host_build_dir('bin/protoc'))
    with patch('mama.build_target.SubProcess.run') as run:
        assert t.build_host_binary('bin/protoc') == binary  # cheap check hit
        run.assert_not_called()


def test_cross_miss_bootstraps_then_returns_the_produced_binary(tmp_path):
    t, dep = _cross_target(tmp_path)
    produced = t.host_build_dir('bin/protoc')
    def fake_child(argv, cwd=None, **kw):
        _touch(produced); return 0   # the `mama <host> build` child produces protoc
    with patch('mama.build_target.SubProcess.run', side_effect=fake_child) as run:
        assert t.build_host_binary('bin/protoc') == produced
    argv = run.call_args.args[0]
    assert argv[0] == sys.executable and argv[-3:] == ['linux', 'build', 'target=libfoo']
    assert run.call_args.kwargs['cwd'] == str(tmp_path)   # root project, so the child resolves the graph


def test_cross_bootstrap_failure_returns_none(tmp_path):
    t, dep = _cross_target(tmp_path)
    with patch('mama.build_target.SubProcess.run', return_value=1):
        assert t.build_host_binary('bin/protoc') is None   # non-zero exit, nothing produced


def test_cross_miss_produced_nothing_returns_none(tmp_path):
    t, dep = _cross_target(tmp_path)
    with patch('mama.build_target.SubProcess.run', return_value=0):  # exit 0 but no binary on disk
        assert t.build_host_binary('bin/protoc') is None


def test_auto_build_false_never_spawns_a_child(tmp_path):
    t, dep = _cross_target(tmp_path)
    with patch('mama.build_target.SubProcess.run') as run:
        assert t.build_host_binary('bin/protoc', auto_build=False) is None
        run.assert_not_called()


def test_windows_host_resolves_the_exe_suffix(tmp_path):
    t, dep = _cross_target(tmp_path)
    binary = _touch(t.host_build_dir('bin/protoc.exe'))
    with patch.object(bt.System, 'windows', True), patch('mama.build_target.SubProcess.run') as run:
        assert t.build_host_binary('bin/protoc') == binary   # 'bin/protoc' -> 'bin/protoc.exe'
        run.assert_not_called()
