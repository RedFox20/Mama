"""Pins a real module build across a mama dependency: the consumer compiles the exported .cppm,
the packaged archive drops the module object, and an incapable toolchain keeps the headers."""
import http.server
import os
import re
import shutil
import tempfile
import threading
import zipfile
from glob import glob
from types import SimpleNamespace

import pytest
from testutils import (executable_extension, init, is_windows, mama_exec, module_capable_compiler,
                       native_platform_name, static_library_extension)

from mama.platforms.windows import Windows
from mama.utils.paths import forward_slashes
from mama.utils.sub_process import execute_piped


def _env(**overrides):
    """Env for one mama run. The compiler follows this host."""
    cc = module_capable_compiler()
    env = {'MAMA_TEST_MODULE_COMPILER': cc['name'], 'MAMA_TEST_CC': cc['cc'], 'MAMA_TEST_CXX': cc['cxx'],
           'MAMA_TEST_CXX_VERSION': cc['version'],
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


def _producer_lib(root) -> str:
    """The producer archive under `root`. A glob, because the name carries a `lib` prefix only on GNU,
    and a multi-config generator writes it into a per-configuration subdir. The shallowest hit is the
    build output, and the deeper ones are the copies that install and deploy made."""
    hits = [forward_slashes(h)
            for h in glob(f'{root}/**/*Producer{static_library_extension()}', recursive=True)]
    deployed = forward_slashes(root) + '/deploy/'  # the packaged copy is a different archive
    hits = sorted((h for h in hits if not h.startswith(deployed)), key=lambda h: h.count('/'))
    assert hits, f'no producer archive under {root}'
    return hits[0]


def _members(lib) -> str:
    """The object members of a static library, through the archiver of this platform. It skips when
    that archiver answers nothing, because an empty listing passes a `not in` assert for free."""
    assert os.path.exists(lib), f'no archive at {lib}'
    # Windows keeps lib.exe off PATH, so it comes from the MSVC toolset, as the production strip reads it
    cmd = Windows(SimpleNamespace(verbose=False)).list_archive_members_cmd(lib) if is_windows() else ['ar', 't', lib]
    if not shutil.which(cmd[0]): pytest.skip(f'{cmd[0]} is not on PATH')
    listing = execute_piped(cmd, throw=False)
    if not listing: pytest.skip(f'{cmd[0]} listed no member of {lib}')
    return listing


def test_a_consumer_imports_the_exported_module(tmp_path):
    # the end-to-end proof, and the only test that catches a missing target_compile_features
    assert _run_consumer(_build(tmp_path)) == 'MODULES hello'


def test_the_forced_standard_reaches_the_real_cmake_cache(tmp_path):
    # the producer CMakeLists names no standard, so only enable_cxx20() can put one in the cache
    project = _build(tmp_path)
    cache = open(f'{_producer_build_dir(project)}/CMakeCache.txt').read()
    # an untyped -D lands as UNINITIALIZED, the same as every other option mama passes
    assert re.search(r'^CMAKE_CXX_STANDARD:\w+=20$', cache, re.M)


def test_the_deployed_package_records_the_module_and_ships_it(tmp_path):
    _, deploy = _build_and_deploy(tmp_path)
    assert 'M include/rpp/rpp-strview.cppm' in open(f'{deploy}/papa.txt').read()
    assert os.path.exists(f'{deploy}/include/rpp/rpp-strview.cppm')


def test_the_packaged_archive_holds_no_module_object(tmp_path):
    _, deploy = _build_and_deploy(tmp_path)
    assert not [m for m in _members(_producer_lib(deploy)).split() if 'rpp-strview' in m]


def test_the_build_dir_archive_keeps_its_module_object(tmp_path):
    # the strip touches the packaged copy alone, so the producer's own build still links
    build, _ = _build_and_deploy(tmp_path)
    assert [m for m in _members(_producer_lib(build)).split() if 'rpp-strview' in m]


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
target_link_options(wa PRIVATE "-Wl,--whole-archive" "{_producer_lib(deploy)}" "-Wl,--no-whole-archive")
''')
    out = str(src / 'build')
    cxx = module_capable_compiler()['cxx']
    assert execute_piped(['cmake', '-B', out, '-G', 'Ninja', f'-DCMAKE_CXX_COMPILER={cxx}', str(src)],
                         throw=False) is not None
    execute_piped(['cmake', '--build', out], throw=False)
    assert os.path.exists(f'{out}/wa'), 'the whole-archive link failed on a duplicate module initializer'
    assert (execute_piped([f'{out}/wa']) or '').strip() == 'hello'


@pytest.mark.linux_host
def test_a_generator_without_module_support_falls_back_to_headers(tmp_path):
    # cmake scans modules under Ninja and Visual Studio, so only a make host loses them here
    project = _build(tmp_path, MAMA_TEST_MODULES='0', MAMA_TEST_NO_NINJA='1')
    assert _run_consumer(project) == 'HEADERS hello'


def _serve_package(package_dir, name) -> tuple:
    """Zip a deployed package and serve it the way artifactory does. Returns (url, the http server).
    The consumer composes the archive name from its own platform and compiler, so the handler answers
    every request with the one zip, keeping the naming rules out of this test."""
    served = tempfile.mkdtemp(prefix='mama_artifactory_')
    archive = os.path.join(served, f'{name}.zip')
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as zip:
        for root, _, files in os.walk(package_dir):
            for f in files:
                path = os.path.join(root, f)
                zip.write(path, os.path.relpath(path, package_dir))

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header('Content-Length', str(os.path.getsize(archive)))
            self.end_headers()
            with open(archive, 'rb') as f: shutil.copyfileobj(f, self.wfile)
        def log_message(self, *args): pass

    server = http.server.HTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f'127.0.0.1:{server.server_port}', server


@pytest.mark.slow
def test_a_consumer_imports_a_module_out_of_an_artifactory_package(tmp_path):
    # the fetched package carries the module as an `M` record, never as a producer source tree
    _, deploy = _build_and_deploy(tmp_path)
    assert 'M include/rpp/rpp-strview.cppm' in open(f'{deploy}/papa.txt').read()

    url, server = _serve_package(deploy, 'Producer-artifactory-test')
    try:
        project = _project(tmp_path / 'consumer', MAMA_TEST_ARTIFACTORY=url,
                           MAMA_TEST_ARTIFACTORY_PKG='Producer-artifactory-test')
        assert mama_exec(['build'], exit_on_fail=False) == 0
        # an unpacked package leaves papa.txt beside the libs, and a source build never writes one
        fetched = glob(f'{project}/packages/Producer/{native_platform_name()}*/papa.txt')
        assert fetched, 'the consumer built no fetched package, so it never read the artifactory zip'
        assert 'M include/rpp/rpp-strview.cppm' in open(fetched[0]).read()
        assert _run_consumer(project) == 'MODULES hello'
    finally:
        server.shutdown()
        os.environ.pop('MAMA_TEST_ARTIFACTORY', None)
        os.environ.pop('MAMA_TEST_ARTIFACTORY_PKG', None)
