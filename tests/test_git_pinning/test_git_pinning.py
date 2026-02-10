import os
import sys

def shell_exec(cmd, exit_on_fail=True, echo=True) -> int:
    if echo: print(f'exec: {cmd}')
    result = os.system(cmd)
    if result != 0 and exit_on_fail:
        print(f'exec failed: code: {result} {cmd}')
        if result >= 255:
            result = 1
        sys.exit(result)
    return result

def file_contains(filepath, text):
    with open(filepath, 'r') as f:
        content = f.read()
    return text in content

def test_git_pinning():
    os.chdir(os.path.dirname(os.path.abspath(__file__)))

    shell_exec("mama clean")

    assert not file_contains(os.path.join('packages', 'ExampleRemote', 'ExampleRemote', 'remote.h'), 'REMOTE_VERSION')
    assert file_contains(os.path.join('packages', 'ExampleRemote2', 'ExampleRemote2', 'remote.h'), 'REMOTE_VERSION 2')
    assert not file_contains(os.path.join('packages', 'ExampleRemote3', 'ExampleRemote3', 'remote.h'), 'REMOTE_VERSION')
    assert file_contains(os.path.join('packages', 'ExampleRemote4', 'ExampleRemote4', 'remote.h'), 'REMOTE_VERSION 2')