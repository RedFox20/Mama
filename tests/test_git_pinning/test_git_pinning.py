import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from testutils import shell_exec, file_contains

def remote_file_contains(dep_name, text):
    return file_contains(os.path.join('packages', dep_name, dep_name, 'remote.h'), text)

def test_git_pinning():
    os.chdir(os.path.dirname(os.path.abspath(__file__)))

    shell_exec("mama clean")

    assert not remote_file_contains('ExampleRemote', 'REMOTE_VERSION')
    assert remote_file_contains('ExampleRemote2', 'REMOTE_VERSION 2')
    assert not remote_file_contains('ExampleRemote3', 'REMOTE_VERSION')
    assert remote_file_contains('ExampleRemote4', 'REMOTE_VERSION 2')