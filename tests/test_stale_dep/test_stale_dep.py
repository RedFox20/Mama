import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from testutils import shell_exec, file_contains, rmtree

def get_dep_path(dep_name):
    return os.path.join('packages', dep_name, dep_name)

# Simulates stale dependency updating
def test_stale_dep():
    os.chdir(os.path.dirname(os.path.abspath(__file__)))

    # Clean state for testing
    if os.path.exists('packages'):
        rmtree('packages')

    dep_dir = get_dep_path('ExampleRemote')
    header = os.path.join(dep_dir, 'remote.h')
    shell_exec('mama build unshallow')
    assert file_contains(header, 'REMOTE_VERSION')

    # Switch to older commit
    old_commit = '4acd9052f27a459314651dd485ae8fa79a04d49d'
    shell_exec(f'cd {dep_dir} && git reset --hard {old_commit}')
    assert not file_contains(header, 'REMOTE_VERSION')

    shell_exec('mama update')
    assert file_contains(header, 'REMOTE_VERSION')
