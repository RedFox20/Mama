import pytest

from testutils import git_init_commit


@pytest.fixture
def repo(tmp_path):
    """A git repo holding one committed source file, which both test files in this dir read."""
    d = tmp_path / 'dep'
    git_init_commit(d, files={'lib.cpp': 'int f(){return 1;}\n'})
    return str(d)
