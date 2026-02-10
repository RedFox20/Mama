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