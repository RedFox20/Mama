from testutils import init, shell_exec, file_contains

def remote_file_contains(dep_name, text):
    return file_contains(f'packages/{dep_name}/{dep_name}/remote.h', text)

# Make sure different git pinning methods work
def test_git_pinning():
    init(__file__, clean_dirs=['packages'])
    shell_exec("mama clean")

    # https://github.com/BatteredBunny/MamaExampleRemote repo has different commits that either do or dont have the REMOTE_VERSION line
    assert not remote_file_contains('ExampleRemote', 'REMOTE_VERSION'), "Tag pinning went wrong"
    assert remote_file_contains('ExampleRemote2', 'REMOTE_VERSION 2'), "Tag pinning went wrong"
    assert not remote_file_contains('ExampleRemote3', 'REMOTE_VERSION'), "Commit pinning went wrong"
    assert remote_file_contains('ExampleRemote4', 'REMOTE_VERSION 2'), "Commit pinning went wrong"
