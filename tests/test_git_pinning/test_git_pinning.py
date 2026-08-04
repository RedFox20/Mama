from testutils import init, mama_exec, file_contains

def remote_file_contains(dep_name, text):
    return file_contains(f'packages/{dep_name}/{dep_name}/remote.h', text)

def test_git_pinning(tmp_path):
    init(__file__, tmp_path)
    mama_exec(['update'])   # clone the pinned deps

    # The MamaExampleRemote commits differ: some hold the REMOTE_VERSION line and some do not.
    assert not remote_file_contains('ExampleRemote', 'REMOTE_VERSION'), "Tag pinning went wrong"
    assert remote_file_contains('ExampleRemote2', 'REMOTE_VERSION 2'), "Tag pinning went wrong"
    assert not remote_file_contains('ExampleRemote3', 'REMOTE_VERSION'), "Commit pinning went wrong"
    assert remote_file_contains('ExampleRemote4', 'REMOTE_VERSION 2'), "Commit pinning went wrong"
