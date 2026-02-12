from testutils import init, shell_exec, file_contains, native_platform_name
from mama.types.git import Git

REPO_URL   = 'https://github.com/BatteredBunny/MamaExampleRemote.git'
OLD_COMMIT = '4acd9052f27a459314651dd485ae8fa79a04d49d'
OLD_SHORT  = OLD_COMMIT[:7]

def get_dep_path(dep_name):
    return f'packages/{dep_name}/{dep_name}'

def get_git_status_path(dep_name):
    return f'packages/{dep_name}/{native_platform_name()}/git_status'

def switch_to_stale_commit(dep_name):
    dep_dir = get_dep_path(dep_name)
    shell_exec(f'cd {dep_dir} && git reset --hard {OLD_COMMIT}')

    status_file = get_git_status_path(dep_name)
    with open(status_file, 'w') as f:
        f.write(Git.format_git_status(REPO_URL, '', '', OLD_SHORT))

# Simulates stale dependency updating
def test_stale_dep():
    init(__file__, clean_dirs=['packages'])

    dep_dir = get_dep_path('ExampleRemote')
    header = f'{dep_dir}/remote.h'
    shell_exec('mama build unshallow')
    assert file_contains(header, 'REMOTE_VERSION'), 'Failed to clone dependency repo'

    switch_to_stale_commit('ExampleRemote')
    assert not file_contains(header, 'REMOTE_VERSION'), 'Failed to switch to stale commit'

    shell_exec('mama update')
    assert file_contains(header, 'REMOTE_VERSION'), "Failed updating to latest commit"
