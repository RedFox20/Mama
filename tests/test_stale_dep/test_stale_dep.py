"""Integration: pins that `mama update` moves a dependency clone reset to a stale commit back to the latest."""
from testutils import init, mama_exec, shell_exec, file_contains, native_platform_name
from mama.types.git import Git
from mama.utils.fileio import write_text_to

def get_dep_path(dep_name):
    return f'packages/{dep_name}/{dep_name}'

def get_git_status_path(dep_name):
    return f'packages/{dep_name}/{native_platform_name()}/git_status'

def switch_to_stale_commit(dep_name, remote):
    """Reset the clone to the old commit and rewrite git_status to match, the way a stale checkout looks."""
    shell_exec(f'cd {get_dep_path(dep_name)} && git reset --hard {remote["old"]}')
    write_text_to(get_git_status_path(dep_name),
                  Git.format_git_status(remote['url'], '', '', remote['old'][:7]))


def test_stale_dep(tmp_path, example_remote):
    init(__file__, tmp_path)

    dep_dir = get_dep_path('ExampleRemote')
    header = f'{dep_dir}/remote.h'
    mama_exec(['build', 'unshallow'])
    assert file_contains(header, 'REMOTE_VERSION'), 'Failed to clone dependency repo'

    switch_to_stale_commit('ExampleRemote', example_remote)
    assert not file_contains(header, 'REMOTE_VERSION'), 'Failed to switch to stale commit'

    mama_exec(['update'])
    assert file_contains(header, 'REMOTE_VERSION'), "Failed updating to latest commit"
