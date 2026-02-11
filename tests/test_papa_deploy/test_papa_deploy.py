import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from testutils import is_linux, shell_exec, file_exists, is_windows

def test_git_pinning():
    os.chdir(os.path.dirname(os.path.abspath(__file__)))

    shell_exec("mama build")
    shell_exec("mama deploy")

    if is_windows():
        assert file_exists(os.path.join('bin', 'ExampleConsumer.exe'))
    else:
        assert file_exists(os.path.join('bin', 'ExampleConsumer'))

    if is_windows():
        platform_name = 'windows'
    elif is_linux():
        platform_name = 'linux'
    else:
        raise Exception("Unsupported platform")

    assert file_exists(os.path.join('packages', 'ExampleConsumer', platform_name, 'deploy', 'ExampleConsumer', 'papa.txt'))