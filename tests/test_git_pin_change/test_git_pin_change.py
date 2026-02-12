import os
from testutils import init, shell_exec, file_contains

def stage(num: int, expects: bool, assert_message: str = ""):
    os.environ['GIT_PIN_CHANGE_TEST'] = str(num)
    shell_exec("mama update")

    result = file_contains('packages/ExampleRemote/ExampleRemote/remote.h', 'REMOTE_VERSION')

    if expects:
        assert result, assert_message
    else:
        assert not result, assert_message

# Test that switches between having REMOTE_VERSION and not to demonstrate that the contents actually change when changing git_tag pins
def test_git_pin_change():
    init(__file__, clean_dirs=['packages'])

    stage(0, False, "Failed to pin to a specific commit")
    stage(1, True, "Failed to update commit pin to a new commit")
    stage(2, False, "Failed to switch from commit pin to tag pin")
    stage(3, True, "Failed to update between tag pins")
