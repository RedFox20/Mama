"""Pins the module half of the generated cmake: the helper, its guard, and the per-package variables."""
import shutil

import pytest
from testutils import is_windows, make_includes_dep, make_includes_target

from mama.buildsys.cmake.mamacmake import mama_cmake_text
from mama.dependency_chain import _get_dependency_cmake_defines
from mama.utils.paths import forward_slashes
from mama.utils.sub_process import execute_piped_echo


# Reads the one gate, with no compiler and no target. The caller names the compiler through -D,
# so one probe covers every floor case.
_PROBE = '''cmake_minimum_required(VERSION 3.20)
project(T NONE)
include(${CMAKE_CURRENT_SOURCE_DIR}/mama.cmake)
message(STATUS "MAMA_PROBE ok=${MAMA_MODULES_AVAILABLE}")
'''


# Reports the generator verdict, which the ninja version decides.
_NINJA_PROBE = '''cmake_minimum_required(VERSION 3.20)
project(T NONE)
include(${CMAKE_CURRENT_SOURCE_DIR}/mama.cmake)
message(STATUS "MAMA_PROBE generator=${MAMA_MODULES_GENERATOR}")
'''


def _probe_floor(tmp_path, *defines) -> str:
    """The output of a real cmake run over the generated helper. Skips when cmake or ninja is absent."""
    if not (shutil.which('cmake') and shutil.which('ninja')): pytest.skip('no cmake or ninja')
    (tmp_path / 'mama.cmake').write_text(_text())
    (tmp_path / 'CMakeLists.txt').write_text(_PROBE)
    # a NONE project resolves no scanner, and the clang arm reads one, so name a file that exists
    status, out = execute_piped_echo(None, ['cmake', '-G', 'Ninja', '-B', f'{tmp_path}/build',
                                            f'-DCMAKE_CXX_COMPILER_CLANG_SCAN_DEPS={tmp_path}/mama.cmake',
                                            *defines, str(tmp_path)], echo=False)
    assert status == 0, out
    return out


def _text(ninja_version='1.11.1') -> str:
    return mama_cmake_text(lambda build_dir: f'set(MAMA_BUILD "{build_dir}")', ninja_version)


def _defines(tmp_path, modules=None, includes=None) -> str:
    target = make_includes_target(str(tmp_path))
    target.exported_includes = includes or [f'{tmp_path}/include']
    target.exported_modules = modules or []
    _, text = _get_dependency_cmake_defines(make_includes_dep(target))
    return text


# --- the helper in mama.cmake -------------------------------------------------

def test_the_helper_applies_the_standard_the_module_scanner_needs():
    # a consumer mamafile that forces no standard still gets the C++20 a module needs
    assert 'target_compile_features(${target} ${scope} cxx_std_20)' in _text()


def test_the_helper_adds_the_modules_as_a_cxx_modules_file_set():
    text = _text()
    assert 'function(mama_target_modules target)' in text
    assert 'FILE_SET mama_modules TYPE CXX_MODULES' in text
    assert 'MAMA_HAS_MODULES=1' in text


def test_the_guard_names_every_toolchain_requirement():
    text = _text()
    for needle in ['VERSION_GREATER_EQUAL 3.28', 'MATCHES "^Visual Studio ([0-9]+)"', 'MATCHES "Ninja"',
                   'MAMA_NINJA_VERSION VERSION_LESS 1.11',  # a dyndep file needs ninja 1.11
                   'CMAKE_MATCH_1 GREATER_EQUAL 17',  # cmake scans modules for VS 2022 and newer only
                   'MAMA_MODULES_MIN_GNU   14', 'MAMA_MODULES_MIN_CLANG 18', 'MAMA_MODULES_MIN_MSVC  1934',
                   'option(MAMA_ENABLE_MODULES']:  # the one lever a consumer turns off
        assert needle in text


def test_an_unmatched_toolchain_still_reads_a_defined_variable():
    # the helper returns early on FALSE, so the variable must exist before the guard runs
    text = _text()
    assert text.index('set(MAMA_MODULES_AVAILABLE FALSE)') < text.index('set(MAMA_MODULES_AVAILABLE TRUE)')


# --- the per-package variables in mama-dependencies.cmake ---------------------

def test_a_dep_with_modules_emits_its_module_list_and_base_dirs(tmp_path):
    text = _defines(tmp_path, [f'{tmp_path}/include/rpp/rpp-strview.cppm'])
    assert 'set(TestLib_MODULES' in text and 'rpp-strview.cppm' in text
    assert 'set(TestLib_MODULES_BASE_DIRS' in text


def test_two_include_roots_that_nest_emit_one_base_dir(tmp_path):
    # cmake refuses a file set whose base dirs contain each other, and the outer dir holds them all
    root = forward_slashes(str(tmp_path))
    text = _defines(tmp_path, [f'{root}/include/top.cppm', f'{root}/include/rpp/rpp-strview.cppm'],
                    includes=[f'{root}/include', f'{root}/include/rpp'])
    base = text.split('MODULES_BASE_DIRS', 1)[1]
    assert f'"{root}/include"' in base and f'"{root}/include/rpp"' not in base


def test_a_dep_without_modules_emits_the_same_text_as_before(tmp_path):
    # an upgrade must reconfigure no existing project, and configure.py hashes this file
    text = _defines(tmp_path)
    assert '_MODULES' not in text


# --- what the review found: nesting, scope and a module with no base dir ------

def test_a_module_under_no_exported_include_reaches_no_cmake_variable(tmp_path):
    # cmake refuses a file set whose FILES sit under no BASE_DIRS, so the module must not ship
    text = _defines(tmp_path, [f'{tmp_path}/elsewhere/rpp-strview.cppm'])
    assert '_MODULES' not in text


def test_the_helper_takes_an_optional_scope():
    # a library that installs itself through install(EXPORT) cannot carry a PUBLIC file set
    text = _text()
    assert 'set(scope PUBLIC)' in text and 'set(scope "${ARGV1}")' in text
    assert 'FILE_SET mama_modules TYPE CXX_MODULES' in text


def test_the_clang_path_needs_the_dependency_scanner():
    # a split clang install ships no clang-scan-deps, and an empty scanner breaks every module build
    assert 'CMAKE_CXX_COMPILER_CLANG_SCAN_DEPS AND EXISTS' in _text()


def test_the_consolidated_base_dirs_drop_a_nesting_between_two_packages(tmp_path):
    # each package is valid alone, and cmake refuses one file set whose base dirs contain each other
    from mama import package
    root = forward_slashes(str(tmp_path))
    outer, inner = f'{root}/repo', f'{root}/repo/modules/foo/include'
    assert package.drop_nested_dirs([inner, outer]) == [outer]


def test_drop_nested_dirs_keeps_two_unrelated_roots(tmp_path):
    root = forward_slashes(str(tmp_path))
    a, b = f'{root}/one/include', f'{root}/two/include'
    from mama import package
    assert package.drop_nested_dirs([b, a]) == [a, b]


# --- the compiler floor, which mama knows and a consumer may override ---------

def test_the_helper_names_the_modules_it_added():
    # a reader of the configure log sees which files reached the file set, not only that some did
    assert 'compiles C++20 modules: ${MAMA_MODULES}' in _text()


def test_the_helper_keeps_every_header_when_the_gate_refuses():
    # MAMA_HAS_MODULES is one define, so the early return has to precede it
    text = _text()
    assert 'using the exported headers' in text
    assert text.index('if(NOT MAMA_MODULES_AVAILABLE OR NOT MAMA_MODULES)') \
        < text.index('target_compile_definitions(${target} ${scope} MAMA_HAS_MODULES=1)')


@pytest.mark.parametrize('version, expected', [('18', 'TRUE'), ('17', 'FALSE')])
def test_the_clang_floor_is_the_version_that_builds_a_module(tmp_path, version, expected):
    out = _probe_floor(tmp_path, '-DCMAKE_CXX_COMPILER_ID=Clang', f'-DCMAKE_CXX_COMPILER_VERSION={version}')
    assert f'MAMA_PROBE ok={expected}' in out


def test_the_lever_refuses_a_toolchain_that_could_build_the_modules(tmp_path):
    out = _probe_floor(tmp_path, '-DCMAKE_CXX_COMPILER_ID=Clang', '-DCMAKE_CXX_COMPILER_VERSION=18',
                       '-DMAMA_ENABLE_MODULES=OFF')
    assert 'MAMA_PROBE ok=FALSE' in out


def test_an_empty_compiler_floor_refuses_instead_of_breaking_the_configure(tmp_path):
    # real cmake, because a text assert cannot see an `if` whose right operand expanded to nothing
    out = _probe_floor(tmp_path, '-DCMAKE_CXX_COMPILER_ID=GNU', '-DCMAKE_CXX_COMPILER_VERSION=14',
                       '-DMAMA_MODULES_MIN_GNU=')
    assert 'MAMA_PROBE ok=FALSE' in out  # an empty floor refuses, a quoted one would pass every compiler


@pytest.mark.parametrize('version, expected', [('1.11.1', 'TRUE'), ('1.10.2', 'FALSE'), ('', 'FALSE')])
def test_the_written_ninja_version_decides_the_generator(tmp_path, version, expected):
    # mama measures ninja once and writes the number, so no configure spawns it again
    if not shutil.which('cmake'): pytest.skip('no cmake')
    (tmp_path / 'mama.cmake').write_text(_text(version))
    (tmp_path / 'CMakeLists.txt').write_text(_NINJA_PROBE)
    status, out = execute_piped_echo(None, ['cmake', '-G', 'Ninja', '-B', f'{tmp_path}/build',
                                            str(tmp_path)], echo=False)
    assert status == 0, out
    assert f'MAMA_PROBE generator={expected}' in out


def test_the_visual_studio_generator_refuses_a_clang_toolset():
    # cmake scans a module graph under that generator with the MSVC toolset alone, never with clang-cl
    assert 'NOT CMAKE_GENERATOR MATCHES "^Visual Studio"' in _text()
