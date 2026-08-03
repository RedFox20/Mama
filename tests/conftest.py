import os
import sys
import pytest

# Tests/ for `import testutils`, project root for `from mama.x import y` -
# saves every new test file from repeating the same sys.path.insert dance.
_here = os.path.dirname(__file__)
_repo_root = os.path.abspath(os.path.join(_here, '..'))
sys.path.insert(0, _here)
sys.path.insert(0, _repo_root)


from testutils import is_linux  # after the sys.path setup above, so tests/ is importable


def pytest_runtest_setup(item):
    """Skip a linux_host test off Linux. MIPS, every Yocto SDK and install-raspi need a Linux host, so
    the code under test raises there instead of answering."""
    if item.get_closest_marker('linux_host') and not is_linux():
        pytest.skip('needs a Linux host')


@pytest.fixture(autouse=True)
def _restore_cwd():
    """Restore the working directory after every test. The integration tests chdir into their own
    project copy, and the session removes that copy later."""
    cwd = os.getcwd()
    yield
    os.chdir(cwd)


@pytest.fixture(autouse=True)
def _disarm_abort():
    """Clear the process-wide abort flag after every test. A test that sets the flag and then fails
    leaves it set. Every later test that spawns a subprocess then dies on that stale flag."""
    yield
    from mama.utils import abort
    abort.clear()


@pytest.fixture
def interactive_terminal(monkeypatch):
    """Pretend stdout is a terminal. pytest captures stdout, so is_headless() reads True by default and
    every progress redraw would throttle away."""
    from mama.utils import system
    monkeypatch.setattr(system, 'is_headless', lambda: False)


@pytest.fixture
def no_cmake_writes(monkeypatch):
    """Silence the two disk writes every execute_unified run does. The scheduler fakes have no build
    dir, so mama.cmake and c_cpp_properties.json have nowhere to go."""
    from mama import dependency_chain as dc
    monkeypatch.setattr(dc, '_save_mama_cmake_and_dependencies_cmake', lambda d: None)
    monkeypatch.setattr(dc, '_save_vscode_compile_commands', lambda d: None)


def pytest_configure(config):
    # tmp_path trees go in the gitignored repo subtree, not system temp, for self-contained
    # CI-identical isolation. The temproot, NOT basetemp: pytest then makes its own numbered and
    # locked pytest-<N> dir per session under it, and keeps the last 3. A fixed basetemp is one exact
    # dir, and pytest WIPES it at session start. A second pytest run then deletes the tmp dirs of the
    # first one while it still runs. An explicit --basetemp or temproot still wins.
    if not config.option.basetemp:
        temproot = os.path.join(_repo_root, '.pytest_tmp')
        os.makedirs(temproot, exist_ok=True)
        os.environ.setdefault('PYTEST_DEBUG_TEMPROOT', temproot)
