"""Pins a real module build across a mama dependency: the consumer compiles the exported .cppm,
the packaged archive drops the module object, and an incapable toolchain keeps the headers."""
import os
from glob import glob

import pytest
from testutils import (executable_extension, init, mama_exec, module_capable_compiler,
                       MODULE_TEST_MIN_CLANG, native_platform_name)

from mama.utils.sub_process import execute_piped


def _env(**overrides):
    """Env for one mama run. The compiler and the clang floor follow this host."""
    cc = module_capable_compiler()
    env = {'MAMA_TEST_MODULE_COMPILER': cc['name'], 'MAMA_TEST_CC': cc['cc'], 'MAMA_TEST_CXX': cc['cxx'],
           'MAMA_TEST_CXX_VERSION': cc['version'], 'MAMA_TEST_MIN_CLANG': str(MODULE_TEST_MIN_CLANG),
           'MAMA_TEST_MODULES': '1', 'MAMA_TEST_NO_NINJA': '0'}
    env.update(overrides)
    return env


def _project(tmp_path, **overrides) -> str:
    """Copy the fixture and set the env. Skips when no toolchain on this host can build a module."""
    if not module_capable_compiler(): pytest.skip('no C++20 module capable toolchain')
    project = init(__file__, tmp_path)
    os.environ.update(_env(**overrides))
    return project


def _build(tmp_path, **overrides) -> str:
    project = _project(tmp_path, **overrides)
    assert mama_exec(['build'], exit_on_fail=False) == 0
    return project


def _producer_build_dir(project) -> str:
    """The producer build dir. Its name carries the compiler, so a glob beats composing the name."""
    roots = glob(f'{project}/packages/Producer/{native_platform_name()}*')
    assert len(roots) == 1, f'expected one producer build dir, got {roots}'
    return roots[0]


def _build_and_deploy(tmp_path) -> tuple:
    """Build, then deploy the producer. Returns (its build dir, its deployed package dir)."""
    project = _build(tmp_path)
    assert mama_exec(['deploy', 'Producer'], exit_on_fail=False) == 0
    build = _producer_build_dir(project)
    return build, f'{build}/deploy/Producer'


def _run_consumer(project) -> str:
    exe = f'{project}/bin/Consumer{executable_extension()}'
    assert os.path.exists(exe), f'the build produced no {exe}'
    return (execute_piped([exe]) or '').strip()


def _members(lib) -> str:
    assert os.path.exists(lib), f'no archive at {lib}'
    return execute_piped(['ar', 't', lib], throw=False) or ''


def test_a_consumer_imports_the_exported_module(tmp_path):
    # the end-to-end proof, and the only test that catches a missing target_compile_features
    assert _run_consumer(_build(tmp_path)) == 'MODULES hello'


def test_the_deployed_package_records_the_module_and_ships_it(tmp_path):
    _, deploy = _build_and_deploy(tmp_path)
    assert 'M include/rpp/rpp-strview.cppm' in open(f'{deploy}/papa.txt').read()
    assert os.path.exists(f'{deploy}/include/rpp/rpp-strview.cppm')


def test_the_packaged_archive_holds_no_module_object(tmp_path):
    _, deploy = _build_and_deploy(tmp_path)
    assert 'rpp-strview.cppm.o' not in _members(f'{deploy}/libProducer.a')


def test_the_build_dir_archive_keeps_its_module_object(tmp_path):
    # the strip touches the packaged copy alone, so the producer's own build still links
    build, _ = _build_and_deploy(tmp_path)
    assert 'rpp-strview.cppm.o' in _members(f'{build}/libProducer.a')


@pytest.mark.linux_host
def test_a_whole_archive_link_of_the_package_finds_one_module_initializer(tmp_path):
    # without the strip this link fails: multiple definition of 'initializer for module rpp.strview'
    _, deploy = _build_and_deploy(tmp_path)
    src = tmp_path / 'wa'
    src.mkdir()
    (src / 'main.cpp').write_text('import rpp.strview;\n#include <cstdio>\n'
                                  'int main(){ std::printf("%s\\n", rpp::greet().c_str()); return 0; }\n')
    # the consumer compiles the exported module itself, and the whole archive also lands in the binary
    (src / 'CMakeLists.txt').write_text(f'''cmake_minimum_required(VERSION 3.28)
project(wa CXX)
add_executable(wa main.cpp)
target_compile_features(wa PRIVATE cxx_std_20)
target_include_directories(wa PRIVATE {deploy}/include)
target_sources(wa PRIVATE FILE_SET CXX_MODULES BASE_DIRS {deploy}/include
               FILES {deploy}/include/rpp/rpp-strview.cppm)
target_link_options(wa PRIVATE "-Wl,--whole-archive" "{deploy}/libProducer.a" "-Wl,--no-whole-archive")
''')
    out = str(src / 'build')
    cxx = module_capable_compiler()['cxx']
    assert execute_piped(['cmake', '-B', out, '-G', 'Ninja', f'-DCMAKE_CXX_COMPILER={cxx}', str(src)],
                         throw=False) is not None
    execute_piped(['cmake', '--build', out], throw=False)
    assert os.path.exists(f'{out}/wa'), 'the whole-archive link failed on a duplicate module initializer'
    assert (execute_piped([f'{out}/wa']) or '').strip() == 'hello'


def test_a_generator_without_module_support_falls_back_to_headers(tmp_path):
    # requirement 4, and it needs no old compiler: cmake scans modules only under Ninja
    project = _build(tmp_path, MAMA_TEST_MODULES='0', MAMA_TEST_NO_NINJA='1')
    assert _run_consumer(project) == 'HEADERS hello'
