import os
from testutils import init, mama_exec, file_contains

def stage(num: int, expects: bool, assert_message: str = ""):
    os.environ['GIT_PIN_CHANGE_TEST'] = str(num)
    mama_exec(['update'])

    result = file_contains('packages/ExampleRemote/ExampleRemote/remote.h', 'REMOTE_VERSION')

    if expects:
        assert result, assert_message
    else:
        assert not result, assert_message

def test_git_pin_change(tmp_path):
    init(__file__, tmp_path)

    stage(0, False, "Failed to pin to a specific commit")
    stage(1, True, "Failed to update commit pin to a new commit")
    stage(2, False, "Failed to switch from commit pin to tag pin")
    stage(3, True, "Failed to update between tag pins")
    stage(4, False, "Failed to switch from tag pin to branch pin")
    stage(5, True, "Failed to update between branch pins")
