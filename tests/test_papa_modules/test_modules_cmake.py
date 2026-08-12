"""Pins the module half of the generated cmake: the helper, its guard, and the per-package variables."""
from testutils import make_includes_dep, make_includes_target

from mama.buildsys.cmake.mamacmake import mama_cmake_text
from mama.dependency_chain import _get_dependency_cmake_defines


def _text() -> str:
    return mama_cmake_text(lambda build_dir: f'set(MAMA_BUILD "{build_dir}")')


def _defines(tmp_path, modules=None) -> str:
    target = make_includes_target(str(tmp_path))
    target.exported_includes = [f'{tmp_path}/include']
    target.exported_modules = modules or []
    _, text = _get_dependency_cmake_defines(make_includes_dep(target))
    return text


# --- the helper in mama.cmake -------------------------------------------------

def test_the_helper_applies_the_standard_the_module_scanner_needs():
    # mama passes -std=c++20 as a raw flag, and a CXX_MODULES file set reads target_compile_features
    assert 'target_compile_features(${target} PUBLIC cxx_std_20)' in _text()


def test_the_helper_adds_the_modules_as_a_cxx_modules_file_set():
    text = _text()
    assert 'function(mama_target_modules target)' in text
    assert 'FILE_SET mama_modules TYPE CXX_MODULES' in text
    assert 'MAMA_HAS_MODULES=1' in text


def test_the_guard_names_every_toolchain_requirement():
    text = _text()
    for needle in ['VERSION_GREATER_EQUAL 3.28', 'Ninja|Visual Studio',
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


def test_a_dep_without_modules_emits_the_same_text_as_before(tmp_path):
    # an upgrade must reconfigure no existing project, and configure.py hashes this file
    text = _defines(tmp_path)
    assert '_MODULES' not in text
