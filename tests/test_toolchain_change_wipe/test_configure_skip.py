"""Pins the `update` configure skip: an unchanged target reconfigures once and never again, and every
input that reaches cmake forces a new one. A missed input here is a silently stale build."""
import os
from unittest.mock import patch
import pytest

from testutils import make_configured_target, write_cmake_cache, write_build_file
from mama.buildsys.cmake import configure as cc

NINJA = 'CMAKE_GENERATOR:INTERNAL=Ninja\n'


def _updating_target(tmp_path, **overrides):
    """A target whose config asks for an update, which is what makes mama reconfigure at all."""
    t, dep = make_configured_target(tmp_path, update=True, run_cmake_configure=False, **overrides)
    return t, dep


def _configure(t, dep):
    """Drive run_config with cmake stubbed. Returns True when it really configured."""
    calls = []
    with patch('mama.buildsys.cmake.configure._rerunnable_cmake_conf', side_effect=lambda *a, **k: calls.append(1)), \
         patch('mama.buildsys.cmake.configure.compute_env', return_value={}), \
         patch('mama.buildsys.cmake.configure._seed_coordinator') as coord, \
         patch.object(dep, 'get_enabled_sanitizers', return_value=''):
        coord.return_value.prepare.return_value = 'none'
        coord.return_value.status.return_value = ('fp', False)
        cc.run_config(t)
        # the stub writes no build system, so stand in for what a real cmake leaves behind
        write_cmake_cache(t.build_dir(), NINJA); write_build_file(t.build_dir())
    return bool(calls)


def _exports(t, text):
    """Write the file that names the include dirs and libs the dependencies export."""
    with open(os.path.join(t.build_dir(), 'mama-dependencies.cmake'), 'w', encoding='utf-8') as f:
        f.write(text)


def test_an_unchanged_target_configures_once_and_then_skips(tmp_path):
    # a warm configure of a real project costs about 50 seconds, and `update` used to force one per target
    t, dep = _updating_target(tmp_path)
    assert _configure(t, dep) is True
    assert _configure(t, dep) is False
    assert _configure(t, dep) is False


def _armed(t, dep):
    """Configure once, then prove the gate now skips. Every test below starts from here, so a later
    `configured` result means the change under test defeated a gate that was working."""
    assert _configure(t, dep) is True
    assert _configure(t, dep) is False


@pytest.mark.parametrize('change', ['cmake option', 'build type', 'install prefix', 'dependency exports'])
def test_every_input_that_reaches_cmake_forces_a_configure(tmp_path, change):
    t, dep = _updating_target(tmp_path)
    _exports(t, 'set(foo_LIBS a.lib)\n')
    _armed(t, dep)
    if change == 'cmake option':        t.cmake_opts = t.cmake_opts + ['NEW_OPTION=1']
    elif change == 'build type':        t.cmake_build_type = 'Debug'
    elif change == 'install prefix':    t.cmake_install_prefix = str(tmp_path / 'elsewhere')
    elif change == 'dependency exports': _exports(t, 'set(foo_LIBS a.lib b.lib)\n')  # a new export lib
    assert _configure(t, dep) is True


def test_a_moved_toolchain_forces_a_configure(tmp_path):
    t, dep = _updating_target(tmp_path)
    _armed(t, dep)
    with patch('mama.buildsys.cmake.configure._toolchain_fingerprint', return_value='OTHER-TOOLCHAIN'):
        assert _configure(t, dep) is True


def test_mama_configure_forces_one_even_when_nothing_changed(tmp_path):
    # `mama configure` is the explicit override, so it must never consult the fingerprint
    t, dep = _updating_target(tmp_path)
    _armed(t, dep)
    dep.config.run_cmake_configure = True
    assert _configure(t, dep) is True


def test_an_invalid_build_dir_is_never_skipped(tmp_path):
    t, dep = _updating_target(tmp_path)
    _armed(t, dep)
    os.remove(os.path.join(t.build_dir(), 'build.ninja'))  # a generator file a killed configure never wrote
    assert _configure(t, dep) is True


def test_a_failed_configure_records_no_fingerprint(tmp_path):
    t, dep = _updating_target(tmp_path)
    with patch('mama.buildsys.cmake.configure._rerunnable_cmake_conf', side_effect=RuntimeError('cmake died')), \
         patch('mama.buildsys.cmake.configure.compute_env', return_value={}), \
         patch('mama.buildsys.cmake.configure._seed_coordinator') as coord, \
         patch.object(dep, 'get_enabled_sanitizers', return_value=''):
        coord.return_value.prepare.return_value = 'none'
        with pytest.raises(RuntimeError):
            cc.run_config(t)
    assert cc._read_configure_fingerprint(t.build_dir()) == ''   # else the next run would skip a broken dir
