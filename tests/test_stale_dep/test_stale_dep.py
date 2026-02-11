from testutils import init, shell_exec, file_contains

def get_dep_path(dep_name):
    return f'packages/{dep_name}/{dep_name}'

# Simulates stale dependency updating
def test_stale_dep():
    init(__file__, clean_dirs=['packages'])

    dep_dir = get_dep_path('ExampleRemote')
    header = f'{dep_dir}/remote.h'
    shell_exec('mama build unshallow')
    assert file_contains(header, 'REMOTE_VERSION'), 'Failed to clone dependency repo'

    # Switch to older commit
    old_commit = '4acd9052f27a459314651dd485ae8fa79a04d49d'
    shell_exec(f'cd {dep_dir} && git reset --hard {old_commit}')
    assert not file_contains(header, 'REMOTE_VERSION'), "Failed to switch to old commit" # Should only fail if something happens to the testing repo

    shell_exec('mama update')
    assert file_contains(header, 'REMOTE_VERSION'), "Failed updating to latest commit"
