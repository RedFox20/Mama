import os
from testutils import init, shell_exec, file_contains, native_platform_name
from mama.types.git import Git

REPO_URL   = 'https://github.com/BatteredBunny/MamaExampleRemote.git'
OLD_COMMIT = '4acd9052f27a459314651dd485ae8fa79a04d49d'
OLD_SHORT  = OLD_COMMIT[:7]

def get_git_status_path():
    return f'packages/ExampleRemote/{native_platform_name()}/git_status'

# Test that mama update does not overwrite local modifications
# and instead shows an error suggesting mama reclone
def test_local_work_protection():
    init(__file__, clean_dirs=['packages'])

    # Clone with branch pin (stage 5 = branch 'master')
    os.environ['GIT_PIN_CHANGE_TEST'] = '5'
    shell_exec("mama update")

    src_file = 'packages/ExampleRemote/ExampleRemote/remote.cpp'
    assert os.path.isfile(src_file), "Source file should exist after clone"

    # Simulate local work by modifying a tracked file
    with open(src_file, 'a') as f:
        f.write('\n// local modification by developer\n')

    # Fake a stale commit in git_status so mama thinks there's an upstream change
    status_file = get_git_status_path()
    with open(status_file, 'w') as f:
        f.write(Git.format_git_status(REPO_URL, '', 'master', OLD_SHORT))

    # mama update should refuse to overwrite local work
    result = shell_exec("mama update", exit_on_fail=False)
    assert result != 0, "mama update should fail when local modifications exist"

    # Verify local work was NOT overwritten
    with open(src_file, 'r') as f:
        current_content = f.read()
    assert '// local modification by developer' in current_content, \
        "Local modifications should be preserved after failed update"


# Test that mama update does not overwrite local modifications even when switching pin types
def test_local_work_protection_on_pin_change():
    init(__file__, clean_dirs=['packages'])

    # Clone with tag pin (stage 2 = tag v1.0.0)
    os.environ['GIT_PIN_CHANGE_TEST'] = '2'
    shell_exec("mama update")

    src_file = 'packages/ExampleRemote/ExampleRemote/remote.cpp'
    assert os.path.isfile(src_file), "Source file should exist after clone"

    # Simulate local work by modifying a tracked file
    with open(src_file, 'a') as f:
        f.write('\n// local work in progress\n')

    # Switch pin type from tag to branch (stage 4 = branch 'old')
    os.environ['GIT_PIN_CHANGE_TEST'] = '4'
    result = shell_exec("mama update", exit_on_fail=False)
    assert result != 0, "mama update should fail when local modifications exist during pin change"

    # Verify local work was NOT overwritten
    with open(src_file, 'r') as f:
        current_content = f.read()
    assert '// local work in progress' in current_content, \
        "Local modifications should be preserved even during pin type change"
