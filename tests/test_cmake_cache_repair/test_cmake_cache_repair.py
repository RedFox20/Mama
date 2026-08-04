"""Pins recovery from a build dir left half-configured by a killed configure (Ctrl+C, fail-fast teardown):
detect the truncated cache or the stage-1 compiler module, wipe it, reconfigure - instead of trusting it."""
import os, pytest
from unittest.mock import patch

from testutils import make_configured_target, write_cmake_cache, write_build_file, run_config_capturing
from mama.buildsys.cmake import configure as cc

COMPLETE = 'CMAKE_GENERATOR:INTERNAL=Unix Makefiles\nCMAKE_BUILD_TYPE:STRING=Release\n'
NINJA = 'CMAKE_GENERATOR:INTERNAL=Ninja\nCMAKE_BUILD_TYPE:STRING=Release\n'
TRUNCATED = '# This is the CMakeCache file.\nCMAKE_BUILD_TYPE:STRING=Release\n'  # killed before the generator line
VS = 'CMAKE_GENERATOR:INTERNAL=Visual Studio 18 2026\nCMAKE_BUILD_TYPE:STRING=Release\n'


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


def test_a_visual_studio_dir_is_valid_with_either_solution_format(tmp_path):
    d = str(tmp_path / 'b')
    write_cmake_cache(d, VS)
    assert not cc.is_cmake_cache_valid(d)                    # configure died before it wrote a solution
    write_build_file(d, 'Foo.slnx')  # VS 18 (2026) with cmake 4.2 writes the XML format
    assert cc.is_cmake_cache_valid(d)
    write_build_file(d, 'Foo.sln')   # an older toolset writes the classic format
    assert cc.is_cmake_cache_valid(d)


def test_a_visual_studio_slnx_dir_skips_the_reconfigure(tmp_path):
    # the .slnx read as a killed configure, so every `mama build` wiped the dir and paid a full rebuild
    t, dep = make_configured_target(tmp_path)
    write_cmake_cache(t.build_dir(), VS); write_build_file(t.build_dir(), 'Foo.slnx')
    assert _run_config_recording(t, dep) == []
    assert os.path.exists(os.path.join(t.build_dir(), 'CMakeCache.txt'))


def test_unknown_generator_is_trusted_not_wiped(tmp_path):
    d = str(tmp_path / 'b')
    write_cmake_cache(d, 'CMAKE_GENERATOR:INTERNAL=Green Hills MULTI\n')
    assert cc.is_cmake_cache_valid(d)  # unknown build file name - let cmake decide, do not wipe blindly


def test_cache_without_generated_build_file_is_repaired(tmp_path):
    # A find_package failure leaves a COMPLETE cache but no build.ninja. A skipped reconfigure then
    # dies with "ninja: error: loading 'build.ninja'" on every later build until the dir is wiped.
    t, dep = make_configured_target(tmp_path)
    write_cmake_cache(t.build_dir(), COMPLETE)
    assert _run_config_recording(t, dep) == ['conf']
    assert not os.path.exists(os.path.join(t.build_dir(), 'CMakeCache.txt'))


def _run_config_recording(t, dep):
    return ['conf'] * len(run_config_capturing(t, dep))


def test_truncated_cache_is_wiped_and_reconfigured(tmp_path):
    t, dep = make_configured_target(tmp_path)
    write_cmake_cache(t.build_dir(), TRUNCATED)
    assert _run_config_recording(t, dep) == ['conf']   # did NOT skip on a cache that merely exists
    assert not os.path.exists(os.path.join(t.build_dir(), 'CMakeCache.txt'))  # the bad cache was dropped


def test_the_reconfigure_reason_reaches_the_target_log(tmp_path):
    # mamabuild.log records only what the sink receives, so a reason that skips it never reaches the log
    t, dep = make_configured_target(tmp_path, print=True)
    write_cmake_cache(t.build_dir(), TRUNCATED)
    notes = []
    run_config_capturing(t, dep, out=notes.append)
    assert any('incomplete build dir' in n for n in notes)


def test_complete_configure_still_skips_the_reconfigure(tmp_path):
    t, dep = make_configured_target(tmp_path)
    write_cmake_cache(t.build_dir(), COMPLETE); write_build_file(t.build_dir(), 'Makefile')
    assert _run_config_recording(t, dep) == []  # nothing broken -> no needless reconfigure
    assert os.path.exists(os.path.join(t.build_dir(), 'CMakeCache.txt'))


def _write_compiler_module(build_dir, ver='4.3.1', abi_done=True):
    """CMakeFiles/<ver>/CMakeCXXCompiler.cmake. Without the ABI line it is the stage-1 module that a
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
    with patch('mama.buildsys.cmake.configure._cmake_version_number', return_value='4.3.1'):  # no `cmake --version` shell-out
        assert _run_config_recording(t, dep) == ['conf']  # the dir looks complete, but cmake would trust the stale module
    assert not os.path.exists(os.path.join(t.build_dir(), 'CMakeFiles'))  # the stage-1 module: wipe, redetect


def test_a_completed_detection_is_left_alone(tmp_path):
    t, dep = make_configured_target(tmp_path)
    write_cmake_cache(t.build_dir(), NINJA); write_build_file(t.build_dir(), 'build.ninja')
    _write_compiler_module(t.build_dir(), abi_done=True)
    with patch('mama.buildsys.cmake.configure._cmake_version_number', return_value='4.3.1'):
        assert _run_config_recording(t, dep) == []  # nothing broken -> no needless reconfigure


def test_rerunnable_error_covers_every_broken_build_dir_flavour():
    assert cc.is_rerunnable_error('Error: could not find CMAKE_GENERATOR in Cache')  # truncated cache
    assert cc.is_rerunnable_error('make: *** Makefile: No such file or directory')   # missing makefile
    assert cc.is_rerunnable_error("ninja: error: loading 'build.ninja': No such file or directory")
    assert not cc.is_rerunnable_error('error: undefined reference to `foo()`')       # a real build error


def test_install_prefix_defaults_to_the_build_dir(tmp_path):
    # every dependency's package()/export_libs() reads artifacts out of its own build dir
    t, _ = make_configured_target(tmp_path)
    assert t.cmake_install_prefix == '.'


def test_add_cmake_options_routes_the_install_prefix_to_its_field(tmp_path):
    # a plain -D would lose: run_config appends CMAKE_INSTALL_PREFIX after cmake_opts
    t, _ = make_configured_target(tmp_path)
    t.add_cmake_options('CMAKE_INSTALL_PREFIX=staging/usr', 'FOO=1')
    assert t.cmake_install_prefix == 'staging/usr'
    assert 'FOO=1' in t.cmake_opts
    assert not any('CMAKE_INSTALL_PREFIX' in o for o in t.cmake_opts)


def test_install_prefix_routing_works_through_a_list_and_strips_quotes(tmp_path):
    t, _ = make_configured_target(tmp_path)
    t.add_cmake_options(['BAR=2', 'CMAKE_INSTALL_PREFIX="AppDir/usr"'])
    assert t.cmake_install_prefix == 'AppDir/usr'
    assert t.cmake_opts == ['BAR=2']


def test_a_similarly_named_option_is_not_captured(tmp_path):
    t, _ = make_configured_target(tmp_path)
    t.add_cmake_options('CMAKE_INSTALL_PREFIX_OVERRIDE=x')
    assert t.cmake_install_prefix == '.'
    assert t.cmake_opts == ['CMAKE_INSTALL_PREFIX_OVERRIDE=x']
