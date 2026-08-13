"""Pins the module half of the generated cmake: the helper, its guard, and the per-package variables."""
from testutils import make_includes_dep, make_includes_target

from mama.buildsys.cmake.mamacmake import mama_cmake_text
from mama.dependency_chain import _get_dependency_cmake_defines
from mama.utils.paths import forward_slashes


def _text() -> str:
    return mama_cmake_text(lambda build_dir: f'set(MAMA_BUILD "{build_dir}")')


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
                   'if(NOT DEFINED MAMA_NINJA_VERSION)',    # one spawn per build dir, not per configure
                   'CMAKE_MATCH_1 GREATER_EQUAL 17',  # cmake scans modules for VS 2022 and newer only
                   'MAMA_MODULES_MIN_GNU   14', 'MAMA_MODULES_MIN_CLANG 21', 'MAMA_MODULES_MIN_MSVC  1934']:
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
