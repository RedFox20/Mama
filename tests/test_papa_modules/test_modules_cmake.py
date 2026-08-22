"""Pins the module half of the generated cmake: the helper, its guard, and the per-package variables."""
import shutil

import pytest
from testutils import make_includes_dep, make_includes_target

from mama import package
from mama.buildsys.cmake.mamacmake import mama_cmake_text
from mama.dependency_chain import _get_dependency_cmake_defines
from mama.utils.paths import forward_slashes
from mama.utils.sub_process import execute_piped_echo


# Reads one variable, with no compiler and no target, so one probe covers every case the caller
# names through -D. `ok` reads the gate, `generator` reads the half the ninja version decides.
_PROBE = '''cmake_minimum_required(VERSION 3.20)
project(T NONE)
include(${CMAKE_CURRENT_SOURCE_DIR}/mama.cmake)
message(STATUS "MAMA_PROBE ok=${MAMA_MODULES_AVAILABLE} generator=${MAMA_MODULES_GENERATOR}")
'''


def _text(ninja_version='1.11.1') -> str:
    return mama_cmake_text(lambda build_dir: f'set(MAMA_BUILD "{build_dir}")', ninja_version)


def _probe(tmp_path, *defines, ninja_version='1.11.1') -> str:
    """The output of a real cmake run over the generated helper. Skips when cmake or ninja is absent."""
    if not (shutil.which('cmake') and shutil.which('ninja')): pytest.skip('no cmake or ninja')
    (tmp_path / 'mama.cmake').write_text(_text(ninja_version))
    (tmp_path / 'CMakeLists.txt').write_text(_PROBE)
    # a NONE project resolves no scanner, and the clang arm reads one, so name a file that exists
    status, out = execute_piped_echo(None, ['cmake', '-G', 'Ninja', '-B', f'{tmp_path}/build',
                                            f'-DCMAKE_CXX_COMPILER_CLANG_SCAN_DEPS={tmp_path}/mama.cmake',
                                            *defines, str(tmp_path)], echo=False)
    assert status == 0, out
    return out


def _defines(tmp_path, modules=None, includes=None) -> str:
    target = make_includes_target(str(tmp_path))
    target.exported_includes = includes or [f'{tmp_path}/include']
    target.exported_modules = modules or []
    _, text = _get_dependency_cmake_defines(make_includes_dep(target))
    return text


# --- the helper and its guard in mama.cmake -----------------------------------

@pytest.mark.parametrize('needle', [
    'target_compile_features(${target} ${scope} cxx_std_20)',  # a consumer that forces no standard
    'function(mama_target_modules target)',
    'FILE_SET mama_modules TYPE CXX_MODULES',
    'MAMA_HAS_MODULES=1',
    'compiles C++20 modules: ${MAMA_MODULES}',  # the log names which files reached the file set
    'using the exported headers',
    'takes PUBLIC or PRIVATE',  # cmake refuses an INTERFACE file set, and names no caller doing it
    'set(scope PUBLIC)', 'set(scope "${ARGV1}")',  # install(EXPORT) cannot carry a PUBLIC file set
    'option(MAMA_ENABLE_MODULES',  # the one lever a consumer turns off
    'VERSION_GREATER_EQUAL 3.28',
    'MATCHES "^Visual Studio ([0-9]+)"', 'MATCHES "Ninja"',
    'MAMA_NINJA_VERSION VERSION_LESS 1.11',  # a dyndep file needs ninja 1.11
    'CMAKE_MATCH_1 GREATER_EQUAL 17',  # cmake scans modules for VS 2022 and newer only
    'MAMA_MODULES_MIN_GNU   14', 'MAMA_MODULES_MIN_CLANG 18', 'MAMA_MODULES_MIN_MSVC  1934',
    'CMAKE_CXX_COMPILER_CLANG_SCAN_DEPS AND EXISTS',  # a split clang install ships no scanner
    'NOT CMAKE_GENERATOR MATCHES "^Visual Studio"',  # that generator scans with the MSVC toolset alone
])
def test_the_generated_helper_names_every_requirement(needle):
    assert needle in _text()


@pytest.mark.parametrize('earlier, later', [
    # the helper returns early on FALSE, so the variable must exist before the guard runs
    ('set(MAMA_MODULES_AVAILABLE FALSE)', 'set(MAMA_MODULES_AVAILABLE TRUE)'),
    # MAMA_HAS_MODULES is one define, so the early return has to precede it
    ('if(NOT MAMA_MODULES_AVAILABLE OR NOT MAMA_MODULES)',
     'target_compile_definitions(${target} ${scope} MAMA_HAS_MODULES=1)'),
])
def test_the_helper_orders_its_guard_before_what_the_guard_protects(earlier, later):
    text = _text()
    assert text.index(earlier) < text.index(later)


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


@pytest.mark.parametrize('modules', [
    None,  # an upgrade must reconfigure no existing project, and configure.py hashes this file
    ['elsewhere/rpp-strview.cppm'],  # cmake refuses a file set whose FILES sit under no BASE_DIRS
])
def test_a_dep_with_no_reachable_module_emits_the_same_text_as_before(tmp_path, modules):
    assert '_MODULES' not in _defines(tmp_path, modules and [f'{tmp_path}/{m}' for m in modules])


@pytest.mark.parametrize('dirs, expected', [
    # each package is valid alone, and cmake refuses one file set whose base dirs contain each other
    (['repo/modules/foo/include', 'repo'], ['repo']),
    (['two/include', 'one/include'], ['one/include', 'two/include']),
])
def test_the_consolidated_base_dirs_drop_only_a_real_nesting(tmp_path, dirs, expected):
    root = forward_slashes(str(tmp_path))
    assert package.drop_nested_dirs([f'{root}/{d}' for d in dirs]) == [f'{root}/{e}' for e in expected]


# --- the compiler floor, read by a real cmake --------------------------------

@pytest.mark.parametrize('defines, ok', [
    (['-DCMAKE_CXX_COMPILER_ID=Clang', '-DCMAKE_CXX_COMPILER_VERSION=18'], 'TRUE'),
    (['-DCMAKE_CXX_COMPILER_ID=Clang', '-DCMAKE_CXX_COMPILER_VERSION=17'], 'FALSE'),
    (['-DCMAKE_CXX_COMPILER_ID=Clang', '-DCMAKE_CXX_COMPILER_VERSION=18',
      '-DMAMA_ENABLE_MODULES=OFF'], 'FALSE'),
    # an empty floor refuses, and a quoted one would pass every compiler. Only a real cmake can see
    # an `if` whose right operand expanded to nothing.
    (['-DCMAKE_CXX_COMPILER_ID=GNU', '-DCMAKE_CXX_COMPILER_VERSION=14', '-DMAMA_MODULES_MIN_GNU='], 'FALSE'),
])
def test_the_gate_answers_the_compiler_and_the_lever(tmp_path, defines, ok):
    assert f'MAMA_PROBE ok={ok}' in _probe(tmp_path, *defines)


@pytest.mark.parametrize('version, expected', [('1.11.1', 'TRUE'), ('1.10.2', 'FALSE'), ('', 'FALSE')])
def test_the_written_ninja_version_decides_the_generator(tmp_path, version, expected):
    # mama measures ninja once and writes the number, so no configure spawns it again
    assert f'generator={expected}' in _probe(tmp_path, ninja_version=version)
