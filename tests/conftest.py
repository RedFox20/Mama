import os
import sys
import pytest

# Tests/ for `import testutils`, project root for `from mama.x import y` -
# saves every new test file from repeating the same sys.path.insert dance.
_here = os.path.dirname(__file__)
_repo_root = os.path.abspath(os.path.join(_here, '..'))
sys.path.insert(0, _here)
sys.path.insert(0, _repo_root)


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
    # tmp_path artifacts go in the gitignored repo subtree (not system temp) for self-contained,
    # CI-identical isolation. pytest wipes it at session start; --basetemp still overrides.
    if not config.option.basetemp:
        config.option.basetemp = os.path.join(_repo_root, '.pytest_tmp')
