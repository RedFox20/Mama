"""Pins a four level package chain: every level exports a module, and the top imports them all."""
import os
import subprocess

import pytest
import testutils
from testutils import executable_extension, module_capable_compiler


def _env() -> dict:
    cc = module_capable_compiler()
    return {**os.environ, 'MAMA_TEST_MODULE_COMPILER': cc['name'], 'MAMA_TEST_CC': cc['cc'],
            'MAMA_TEST_CXX': cc['cxx'], 'MAMA_TEST_CXX_VERSION': cc['version']}


@pytest.mark.slow
def test_a_four_level_chain_resolves_every_module_it_imports(tmp_path):
    # top -> LibConsumer2 -> LibConsumer1 -> ReCpp. Each middle archive compiles the modules below,
    # so an unstripped grandchild initializer breaks this link.
    if not module_capable_compiler(): pytest.skip('no C++20 module capable toolchain')
    project = testutils.init(__file__, tmp_path)
    build = subprocess.run(['mama', 'build', 'jobs=3'], cwd=project, env=_env(),
                           capture_output=True, text=True)
    assert build.returncode == 0, build.stdout + build.stderr

    exe = f'{project}/bin/TopConsumer{executable_extension()}'
    assert os.path.exists(exe), 'mama built no top consumer executable'
    app = subprocess.run([exe], capture_output=True, text=True)
    assert app.returncode == 0, app.stdout + app.stderr
    # strict: a headers build links no module initializer at all, so it proves nothing here
    assert 'OK: MODULES' in app.stdout, app.stdout
