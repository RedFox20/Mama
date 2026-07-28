"""Pins the flags mama puts on the cmake configure command line."""
import os
from unittest.mock import Mock

from testutils import make_configured_target, run_config_capturing, write_cmake_cache
from mama import cmake_configure as cc

_MAMA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'mama')


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
    assert cc.use_toolchain_file(dep.config, '/ndk/android.toolchain.cmake') \
        == 'CMAKE_TOOLCHAIN_FILE="/ndk/android.toolchain.cmake"'
    assert dep.config.cmake_toolchain_file == '/ndk/android.toolchain.cmake'


def test_every_toolchain_file_option_goes_through_the_helper():
    """Formatting the option anywhere else would leave cmake_toolchain_file unset, and mama would pass
    CMAKE_C_COMPILER again - the exact bug that made a seeded android build re-detect as the host."""
    hits = []
    for root, _, files in os.walk(_MAMA_DIR):
        for f in files:
            if not f.endswith('.py'): continue
            path = os.path.join(root, f)
            for n, line in enumerate(open(path, encoding='utf-8'), 1):
                if 'CMAKE_TOOLCHAIN_FILE="' in line:
                    hits.append(f'{os.path.relpath(path, _MAMA_DIR)}:{n}')
    files = sorted({h.split(':')[0] for h in hits})
    assert files == ['cmake_configure.py'], f'format it via use_toolchain_file(), not inline: {hits}'


def test_a_toolchain_file_build_does_not_name_the_compiler(tmp_path):
    """The NDK toolchain rewrites our `bin/aarch64-linux-android29-clang` to `bin/clang`. On a build dir
    that already holds a cache our -D then reads as a changed variable, so cmake DELETES the cache and
    re-runs - losing the seeded platform info and re-detecting as the host."""
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
    dep.config.platform = Mock(build_system='make', get_cmake_build_opts=lambda t:
                               ['MIPS=TRUE', cc.use_toolchain_file(dep.config, '/opt/mips/toolchain.cmake')])
    dep.config.msvc = dep.config.linux = False
    assert '-DCMAKE_C_COMPILER=' not in run_config_capturing(t, dep)[0]


def test_a_toolchain_file_build_never_reports_the_compiler_as_moved(tmp_path):
    """Its cache holds the toolchain's own choice, which never equals ours - comparing them would wipe
    every warm cross-build dir on every run."""
    t, dep = make_configured_target(tmp_path, cmake_toolchain_file='/ndk/android.toolchain.cmake',
                                    compiler=('/opt/ndk-B/clang', '/opt/ndk-B/clang++', '21'))
    write_cmake_cache(t.build_dir(), 'CMAKE_GENERATOR:INTERNAL=Ninja\n'
                                     'CMAKE_C_COMPILER:STRING=/opt/ndk-A/clang\n')
    assert cc._toolchain_moved_unfingerprinted(t.build_dir(), t) is False
