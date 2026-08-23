"""Pins the flags mama puts on the cmake configure command line."""
import pytest

from testutils import (make_configured_target, run_config_capturing, write_cmake_cache, set_mock_platform,
                       configure_cmd)
from mama.platforms.mips import Mips
from mama.platforms.windows import Windows
from mama.buildsys.cmake import configure as cc
from mama.buildsys.cmake.options import use_toolchain_file



def test_unused_cli_variables_are_not_warned_about(tmp_path):
    """mama always passes CMAKE_C_COMPILER, so every C++-only project reports it as unused. The warning
    describes mama, not the project, so it is noise in every build log."""
    t, dep = make_configured_target(tmp_path)
    assert '--no-warn-unused-cli' in run_config_capturing(t, dep)[0]


def test_verbose_keeps_the_unused_variable_warning(tmp_path):
    # under verbose it is the only signal that an add_cmake_options() name is misspelled
    t, dep = make_configured_target(tmp_path, verbose=True)
    assert '--no-warn-unused-cli' not in run_config_capturing(t, dep)[0]


# --- a toolchain file owns compiler selection ---

def test_use_toolchain_file_records_and_formats(tmp_path):
    """The one contract every platform relies on: record the path, hand back the cmake option."""
    _, dep = make_configured_target(tmp_path)
    assert use_toolchain_file(dep.config, '/ndk/android.toolchain.cmake') \
        == 'CMAKE_TOOLCHAIN_FILE="/ndk/android.toolchain.cmake"'
    assert dep.config.cmake_toolchain_file == '/ndk/android.toolchain.cmake'


def test_a_toolchain_file_build_does_not_name_the_compiler(tmp_path):
    """The NDK toolchain rewrites our `bin/aarch64-linux-android29-clang` to `bin/clang`. On a build dir
    that already holds a cache our -D then reads as a changed variable. cmake DELETES the cache and
    re-runs, loses the seeded platform info, and re-detects as the host."""
    t, dep = make_configured_target(tmp_path, cmake_toolchain_file='/ndk/android.toolchain.cmake')
    cmd = run_config_capturing(t, dep)[0]
    assert '-DCMAKE_C_COMPILER=' not in cmd and '-DCMAKE_CXX_COMPILER=' not in cmd


def test_a_native_build_still_names_the_compiler(tmp_path):
    t, dep = make_configured_target(tmp_path)
    cmd = run_config_capturing(t, dep)[0]
    assert '-DCMAKE_C_COMPILER=' in cmd and '-DCMAKE_CXX_COMPILER=' in cmd


def test_the_platform_records_its_toolchain_before_the_compiler_is_decided(tmp_path):
    """Ordering pin: _platform_opts is what calls use_toolchain_file, so it MUST run before
    _set_compiler_paths reads the flag. Swap the two and this build starts naming the compiler again."""
    t, dep = make_configured_target(tmp_path)
    mips = set_mock_platform(dep.config, Mips)
    mips.gcc_prefix = '/opt/mips/bin/mipsel-linux-gnu-'
    mips.toolchain_file = '/opt/mips/toolchain.cmake'
    assert '-DCMAKE_C_COMPILER=' not in run_config_capturing(t, dep)[0]


# --- the MSVC runtime library ---

def _msvc_configure_cmd(tmp_path, cmake_opts=(), **cfg) -> str:
    return configure_cmd(tmp_path, '-G "Visual Studio 18 2026" -A x64', Windows, cmake_opts,
                         msvc=True, linux=False, gcc=False, **cfg)


@pytest.mark.parametrize('debug', [False, True])
def test_msvc_takes_the_release_runtime_from_the_command_line(tmp_path, debug):
    """Policy CMP0091 moved the runtime library out of the per-config flags mama.cmake rewrites.
    Every project on cmake 3.15 or later then linked the debug CRT, and no debug build linked."""
    cmd = _msvc_configure_cmd(tmp_path, debug=debug, release=not debug)
    assert f'-D{cc._MSVC_RUNTIME}={cc._RELEASE_CRT}' in cmd
    assert '-D_ITERATOR_DEBUG_LEVEL=0' in cmd
    # cmake reads the runtime library only under CMP0091 NEW, which a pre-3.15 project does not get
    assert '-DCMAKE_POLICY_DEFAULT_CMP0091=NEW' in cmd


@pytest.mark.parametrize('opt', [
    f'{cc._MSVC_RUNTIME}=MultiThreadedDebugDLL',   # the debug heap
    f'  {cc._MSVC_RUNTIME} ="MultiThreaded"',      # the static CRT, quoted, with whitespace around the key
])
def test_a_mamafile_cannot_take_a_runtime_library_of_its_own(tmp_path, opt, capsys):
    """One CRT and one iterator level across the tree is what lets a debug root link a release
    dependency. A target that diverged would fail the link, or corrupt the heap across a DLL."""
    cmd = _msvc_configure_cmd(tmp_path, cmake_opts=[opt])
    last = cmd.rindex(f'{cc._MSVC_RUNTIME}=')  # mama appends last, so cmake reads its value
    assert cmd[last:].startswith(f'{cc._MSVC_RUNTIME}={cc._RELEASE_CRT}')
    assert '-D_ITERATOR_DEBUG_LEVEL=0' in cmd
    assert f'ignores {cc._MSVC_RUNTIME}' in capsys.readouterr().out


@pytest.mark.parametrize('opt', [
    f'{cc._MSVC_RUNTIME}={cc._RELEASE_CRT}',   # the same CRT mama passes: redundant, not a conflict
    f'{cc._MSVC_RUNTIME}_EXTRA=ON',            # a different key that only starts the same way
])
def test_an_option_that_conflicts_with_nothing_draws_no_warning(tmp_path, opt, capsys):
    cmd = _msvc_configure_cmd(tmp_path, cmake_opts=[opt])
    assert f'-D{cc._MSVC_RUNTIME}={cc._RELEASE_CRT}' in cmd and '-D_ITERATOR_DEBUG_LEVEL=0' in cmd
    assert 'ignores' not in capsys.readouterr().out


def test_a_non_msvc_build_names_no_msvc_runtime(tmp_path):
    t, dep = make_configured_target(tmp_path)
    cmd = run_config_capturing(t, dep)[0]
    assert 'CMAKE_MSVC_RUNTIME' not in cmd and 'CMP0091' not in cmd and '_ITERATOR_DEBUG_LEVEL' not in cmd


def test_a_toolchain_file_build_never_reports_the_compiler_as_moved(tmp_path):
    """Its cache holds the toolchain's own choice, which never equals ours - comparing them would wipe
    every warm cross-build dir on every run."""
    t, dep = make_configured_target(tmp_path, cmake_toolchain_file='/ndk/android.toolchain.cmake',
                                    compiler=('/opt/ndk-B/clang', '/opt/ndk-B/clang++', '21'))
    write_cmake_cache(t.build_dir(), 'CMAKE_GENERATOR:INTERNAL=Ninja\n'
                                     'CMAKE_C_COMPILER:STRING=/opt/ndk-A/clang\n')
    assert cc._toolchain_moved_unfingerprinted(t.build_dir(), t) is False


# --- option spelling and the standard the compiler really reads ----------------

def test_an_option_that_spells_its_own_prefix_names_the_right_variable():
    # two prefixes name a cmake variable literally called `-DFOO`, and the project never sees FOO
    assert cc._opts_to_defines(['-DFOO=1', 'BAR=2']) == '-DFOO=1 -DBAR=2 '


def test_the_last_std_flag_decides_the_standard(tmp_path):
    # the compiler reads the last -std of the line, so an earlier one does not decide the build
    target, _ = make_configured_target(tmp_path)
    target.config.flags = '-std=c++17 -std=c++20'
    target.enable_cxx20()
    assert 'CMAKE_CXX_STANDARD=20' in cc._cxx_standard_opts(target)


def test_a_standard_spelling_inside_a_macro_value_is_not_an_operator_flag(tmp_path):
    # an unanchored search read the macro value as the operator standard, and cmake then appended a
    # C++17 flag after the C++23 one the mamafile forced
    target, _ = make_configured_target(tmp_path)
    target.config.flags = '-DDEFAULT_STD=-std=c++17'
    target.enable_cxx23()
    assert 'CMAKE_CXX_STANDARD=23' in cc._cxx_standard_opts(target)
