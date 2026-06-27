import os
import sys

# Tests/ for `import testutils`, project root for `from mama.x import y` -
# saves every new test file from repeating the same sys.path.insert dance.
_here = os.path.dirname(__file__)
_repo_root = os.path.abspath(os.path.join(_here, '..'))
sys.path.insert(0, _here)
sys.path.insert(0, _repo_root)


def pytest_configure(config):
    # Keep all tmp_path artifacts inside the (gitignored) repo subtree rather than the system
    # temp dir, so test isolation is self-contained and identical on any CI machine. cwd-independent
    # (anchored to this file). pytest wipes this dir at session start. --basetemp still overrides.
    if not config.option.basetemp:
        config.option.basetemp = os.path.join(_repo_root, '.pytest_tmp')
