"""Pins that mama clones a well known C++ package, builds it, and links an app against it.

The app imports the C++20 module when that package exports one, and includes the header when it does
not, so this test follows whichever path the package and the toolchain allow. This is the one test
that reaches the network, because a real dependency is the point of it.
Excluded from the default run: `python -m pytest tests/test_recpp_consumer -m slow`."""
import os
import shutil
import socket
import subprocess

import pytest
import testutils

RECPP_HOST = 'github.com'


def _has_network() -> bool:
    """The clone needs the host that serves the package. An offline run skips instead of failing."""
    try:
        socket.create_connection((RECPP_HOST, 443), timeout=5).close()
        return True
    except OSError:
        return False


def _run(cmd, env=None) -> subprocess.CompletedProcess:
    """Raw subprocess.run, not SubProcess.run: this test reads the whole output after the fact and
    needs no live progress display."""
    print('+', ' '.join(cmd), flush=True)
    return subprocess.run(cmd, text=True, capture_output=True, env=env)


def _build_and_run(project, compiler, env) -> str:
    """Build the consumer with mama and return what the app printed."""
    shutil.rmtree(os.path.join(project, 'packages'), ignore_errors=True)
    build = _run(['mama', compiler, 'build', 'jobs=3'], env=env)
    assert build.returncode == 0, build.stdout + build.stderr
    exe = next((os.path.join(root, name)
                for root, _, files in os.walk(os.path.join(project, 'packages', 'consumer'))
                for name in files if name in ('consumer', 'consumer.exe')), '')
    assert exe, 'mama built no consumer executable'
    app = _run([exe], env=env)
    print(app.stdout)
    assert app.returncode == 0, app.stdout + app.stderr
    assert 'OK:' in app.stdout, app.stdout
    return app.stdout


def _env(**extra) -> dict:
    env = {**os.environ, **extra}
    if not os.path.exists('/usr/lib/llvm-18/include/c++/v1'): env['USE_GCC_STDLIB'] = '1'
    return env


@pytest.mark.slow
@pytest.mark.parametrize('compiler', ['windows'] if testutils.is_windows() else ['clang', 'gcc'])
def test_mama_clones_builds_and_consumes_a_real_package(tmp_path, compiler):
    if not (shutil.which('cmake') and shutil.which('ninja')): pytest.skip('no cmake or ninja')
    if not _has_network(): pytest.skip(f'{RECPP_HOST} is unreachable, so the package cannot clone')
    project = testutils.init(__file__, tmp_path)
    out = _build_and_run(project, compiler, _env())

    took = 'modules' if 'built with MODULES' in out else 'headers'
    print(f'the consumer took the {took} path')

    # One compiler, both paths, one report. A facade that re-exports through using-declarations is
    # where a compiler tends to differ, and only this comparison can see that.
    if took == 'modules':
        other = _build_and_run(project, compiler, _env(NO_MODULES='1'))
        assert other.splitlines()[1:] == out.splitlines()[1:], 'the two paths disagree'
