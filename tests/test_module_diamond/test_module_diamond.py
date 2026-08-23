"""Pins the diamond: two sibling packages compile one module, and one consumer links both."""
import glob
import os
import shutil
import subprocess

import pytest
import testutils
from testutils import executable_extension, module_capable_compiler


def _env() -> dict:
    cc = module_capable_compiler()
    return {**os.environ, 'MAMA_TEST_MODULE_COMPILER': cc['name'], 'MAMA_TEST_CC': cc['cc'],
            'MAMA_TEST_CXX': cc['cxx'], 'MAMA_TEST_CXX_VERSION': cc['version']}


def _one(pattern: str) -> str:
    """The single path `pattern` matches. Two matches for one package name is the broken build."""
    hits = glob.glob(pattern)
    assert len(hits) == 1, f'{pattern} matched {hits}'
    return hits[0]


def _members(archive: str) -> str:
    return subprocess.run(['ar', 't', archive], capture_output=True, text=True).stdout


@pytest.mark.slow
def test_two_siblings_that_compile_one_module_link_into_one_consumer(tmp_path):
    # LibA and LibB each compile shared.api into their own archive, so their initializers collide with
    # each other before the consumer's own copy joins the link. A linear chain never makes that pair.
    if not module_capable_compiler(): pytest.skip('no C++20 module capable toolchain')
    project = testutils.init(__file__, tmp_path)
    build = subprocess.run(['mama', 'build', 'jobs=3'], cwd=project, env=_env(),
                           capture_output=True, text=True)
    assert build.returncode == 0, build.stdout + build.stderr

    exe = f'{project}/bin/TopConsumer{executable_extension()}'
    assert os.path.exists(exe), 'mama built no top consumer executable'
    app = subprocess.run([exe], capture_output=True, text=True)
    assert app.returncode == 0, app.stdout + app.stderr
    # strict: a headers build proves nothing about a duplicate initializer, so it fails here
    assert 'OK: MODULES' in app.stdout, app.stdout

    # one package below two parents stays one package. Two would each carry their own module objects,
    # and the consumer would compile and link the same interface twice.
    assert len(glob.glob(f'{project}/packages/LibShared/*/')) == 1, 'the diamond built LibShared twice'

    if not shutil.which('ar'): return  # MSVC keeps its members through lib.exe, which lists differently
    for side in ('LibA', 'LibB'):
        built = _one(f'{project}/packages/{side}/*/lib{side}.a')
        # the precondition: without it a change that stops compiling the module leaves this test green
        assert 'shared-api.cppm.o' in _members(built), f'{side} never compiled the shared module'
        stripped = _one(f'{project}/packages/{side}/*/mama-nomodules/lib{side}.a')
        assert '.cppm.o' not in _members(stripped), f'{side} published a module object'
