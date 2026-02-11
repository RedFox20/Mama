import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from testutils import shell_exec, file_contains

def get_dep_path(dep_name):
    return os.path.join('packages', dep_name, dep_name)

def test_git_pinning():
    os.chdir(os.path.dirname(os.path.abspath(__file__)))

    shell_exec("mama clean")

    assert not file_contains(os.path.join(get_dep_path('ExampleRemote'), 'remote.h'), 'REMOTE_VERSION')
    assert file_contains(os.path.join(get_dep_path('ExampleRemote2'), 'remote.h'), 'REMOTE_VERSION 2')
    assert not file_contains(os.path.join(get_dep_path('ExampleRemote3'), 'remote.h'), 'REMOTE_VERSION')
    assert file_contains(os.path.join(get_dep_path('ExampleRemote4'), 'remote.h'), 'REMOTE_VERSION 2')