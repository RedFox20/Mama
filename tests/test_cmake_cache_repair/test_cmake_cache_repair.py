"""Pins recovery from a build dir left half-configured by a killed configure (Ctrl+C, fail-fast teardown):
detect the truncated cache or the stage-1 compiler module, wipe it, reconfigure - instead of trusting it."""
import os, pytest
from unittest.mock import patch

from testutils import make_configured_target, write_cmake_cache, write_build_file
from mama import cmake_configure as cc

COMPLETE = 'CMAKE_GENERATOR:INTERNAL=Unix Makefiles\nCMAKE_BUILD_TYPE:STRING=Release\n'
NINJA = 'CMAKE_GENERATOR:INTERNAL=Ninja\nCMAKE_BUILD_TYPE:STRING=Release\n'
TRUNCATED = '# This is the CMakeCache file.\nCMAKE_BUILD_TYPE:STRING=Release\n'  # killed before the generator line


def test_is_cmake_cache_valid(tmp_path):
    d = str(tmp_path / 'b')
    assert not cc.is_cmake_cache_valid(d)                      # no cache at all
    write_cmake_cache(d, TRUNCATED); assert not cc.is_cmake_cache_valid(d)   # interrupted configure
    write_cmake_cache(d, COMPLETE)
    assert not cc.is_cmake_cache_valid(d)   # complete cache but configure died before emitting the Makefile
    write_build_file(d, 'Makefile'); assert cc.is_cmake_cache_valid(d)  # a configure that ran to completion


def test_cache_generator_reads_the_exact_key():
    assert cc.cache_generator(NINJA) == 'Ninja'
    assert cc.cache_generator(COMPLETE) == 'Unix Makefiles'
    # the _PLATFORM/_TOOLSET siblings must not be mistaken for the generator itself
    assert cc.cache_generator('CMAKE_GENERATOR_PLATFORM:STRING=x64\n') == ''
    assert cc.cache_generator(TRUNCATED) == ''


def test_a_stale_other_build_system_file_does_not_count(tmp_path):
    # Targets pick their own build system: a leftover Makefile must NOT make a Ninja-configured dir
    # look complete, or `cmake --build` dies on the missing build.ninja every time.
    d = str(tmp_path / 'b')
    write_cmake_cache(d, NINJA); write_build_file(d, 'Makefile')
    assert not cc.is_cmake_cache_valid(d)
    write_build_file(d, 'build.ninja'); assert cc.is_cmake_cache_valid(d)


def test_unknown_generator_is_trusted_not_wiped(tmp_path):
    d = str(tmp_path / 'b')
    write_cmake_cache(d, 'CMAKE_GENERATOR:INTERNAL=Green Hills MULTI\n')
    assert cc.is_cmake_cache_valid(d)  # we don't know its build file - let cmake decide, don't wipe blindly


def test_cache_without_generated_build_file_is_repaired(tmp_path):
    # A find_package failure leaves a COMPLETE cache but no build.ninja; skipping the reconfigure then
    # dies with "ninja: error: loading 'build.ninja'" on every later build until it's wiped.
    t, dep = make_configured_target(tmp_path)
    write_cmake_cache(t.build_dir(), COMPLETE)
    assert _run_config_recording(t, dep) == ['conf']
    assert not os.path.exists(os.path.join(t.build_dir(), 'CMakeCache.txt'))


def _run_config_recording(t, dep):
    """run_config with the cmake call + seed coordinator stubbed; returns the recorded conf calls."""
    calls = []
    with patch('mama.cmake_configure._rerunnable_cmake_conf', side_effect=lambda *a, **k: calls.append('conf')), \
         patch('mama.cmake_configure.compute_env', return_value={}), \
         patch('mama.cmake_configure._seed_coordinator') as coord, \
         patch.object(dep, 'get_enabled_sanitizers', return_value=''):
        coord.return_value.prepare.return_value = 'none'
        coord.return_value.status.return_value = ('fp', False)
        cc.run_config(t)
    return calls


def test_truncated_cache_is_wiped_and_reconfigured(tmp_path):
    t, dep = make_configured_target(tmp_path)
    write_cmake_cache(t.build_dir(), TRUNCATED)
    assert _run_config_recording(t, dep) == ['conf']   # did NOT skip on a cache that merely exists
    assert not os.path.exists(os.path.join(t.build_dir(), 'CMakeCache.txt'))  # the bad cache was dropped


def test_complete_configure_still_skips_the_reconfigure(tmp_path):
    t, dep = make_configured_target(tmp_path)
    write_cmake_cache(t.build_dir(), COMPLETE); write_build_file(t.build_dir(), 'Makefile')
    assert _run_config_recording(t, dep) == []  # nothing broken -> no needless reconfigure
    assert os.path.exists(os.path.join(t.build_dir(), 'CMakeCache.txt'))


def _write_compiler_module(build_dir, ver='4.3.1', abi_done=True):
    """CMakeFiles/<ver>/CMakeCXXCompiler.cmake; without the ABI line it's the stage-1 module a
    configure killed mid-detection leaves behind."""
    d = os.path.join(build_dir, 'CMakeFiles', ver); os.makedirs(d, exist_ok=True)
    text = 'set(CMAKE_CXX_COMPILER "/usr/bin/g++")\n' + ('set(CMAKE_CXX_ABI_COMPILED TRUE)\n' if abi_done else '')
    with open(os.path.join(d, 'CMakeCXXCompiler.cmake'), 'w', encoding='utf-8') as f: f.write(text)
    return d


@pytest.mark.parametrize('with_cache', [True, False])  # a kill mid-detection often saves no cache at all
def test_killed_detection_is_wiped_and_reconfigured(tmp_path, with_cache):
    t, dep = make_configured_target(tmp_path)
    if with_cache: write_cmake_cache(t.build_dir(), NINJA); write_build_file(t.build_dir(), 'build.ninja')
    _write_compiler_module(t.build_dir(), abi_done=False)
    with patch('mama.cmake_configure._cmake_version_number', return_value='4.3.1'):  # no `cmake --version` shell-out
        assert _run_config_recording(t, dep) == ['conf']  # the dir looks complete but cmake would trust
    assert not os.path.exists(os.path.join(t.build_dir(), 'CMakeFiles'))  # the stage-1 module: wipe, redetect


def test_a_completed_detection_is_left_alone(tmp_path):
    t, dep = make_configured_target(tmp_path)
    write_cmake_cache(t.build_dir(), NINJA); write_build_file(t.build_dir(), 'build.ninja')
    _write_compiler_module(t.build_dir(), abi_done=True)
    with patch('mama.cmake_configure._cmake_version_number', return_value='4.3.1'):
        assert _run_config_recording(t, dep) == []  # nothing broken -> no needless reconfigure


def test_rerunnable_error_covers_every_broken_build_dir_flavour():
    assert cc.is_rerunnable_error('Error: could not find CMAKE_GENERATOR in Cache')  # truncated cache
    assert cc.is_rerunnable_error('make: *** Makefile: No such file or directory')   # missing makefile
    assert cc.is_rerunnable_error("ninja: error: loading 'build.ninja': No such file or directory")
    assert not cc.is_rerunnable_error('error: undefined reference to `foo()`')       # a real build error
