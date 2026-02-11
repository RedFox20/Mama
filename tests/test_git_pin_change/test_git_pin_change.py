import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from testutils import shell_exec, file_contains

def stage(num: str, expects: bool):
    os.environ['GIT_PIN_CHANGE_TEST'] = num
    shell_exec("mama update")

    result = file_contains(os.path.join('packages', 'ExampleRemote', 'ExampleRemote', 'remote.h'), 'REMOTE_VERSION')

    if expects:
        assert result
    else:
        assert not result

def test_git_pin_update():
    os.chdir(os.path.dirname(os.path.abspath(__file__)))

    # Stages 0-3, switching between having REMOTE_VERSION and not to demonstrate that the contents actually change
    for i in range(4):
        stage(str(i), i % 2 == 1)