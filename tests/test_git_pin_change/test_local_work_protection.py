import os
from testutils import init, mama_exec, native_platform_name
from mama.types.git import Git
from mama.utils.fileio import write_text_to

def get_git_status_path():
    return f'packages/ExampleRemote/{native_platform_name()}/git_status'

def test_local_work_protection(tmp_path, example_remote):
    init(__file__, tmp_path)

    # stage 5 = branch 'master'
    os.environ['GIT_PIN_CHANGE_TEST'] = '5'
    mama_exec(['update'])

    src_file = 'packages/ExampleRemote/ExampleRemote/remote.cpp'
    assert os.path.isfile(src_file), "Source file should exist after clone"

    with open(src_file, 'a') as f:
        f.write('\n// local modification by developer\n')

    # Fake a stale commit in git_status so mama sees an upstream change.
    write_text_to(get_git_status_path(),
                  Git.format_git_status(example_remote['url'], '', 'master', example_remote['old'][:7]))

    result = mama_exec(['update'], exit_on_fail=False)
    assert result != 0, "mama update should fail when local modifications exist"

    with open(src_file, 'r') as f:
        current_content = f.read()
    assert '// local modification by developer' in current_content, \
        "Local modifications should be preserved after failed update"


def test_local_work_protection_on_pin_change(tmp_path, example_remote):
    init(__file__, tmp_path)

    # stage 2 = tag v1.0.0
    os.environ['GIT_PIN_CHANGE_TEST'] = '2'
    mama_exec(['update'])

    src_file = 'packages/ExampleRemote/ExampleRemote/remote.cpp'
    assert os.path.isfile(src_file), "Source file should exist after clone"

    with open(src_file, 'a') as f:
        f.write('\n// local work in progress\n')

    # Switch the pin type from tag to branch (stage 4 = branch 'old').
    os.environ['GIT_PIN_CHANGE_TEST'] = '4'
    result = mama_exec(['update'], exit_on_fail=False)
    assert result != 0, "mama update should fail when local modifications exist during pin change"

    with open(src_file, 'r') as f:
        current_content = f.read()
    assert '// local work in progress' in current_content, \
        "Local modifications should be preserved even during pin type change"
