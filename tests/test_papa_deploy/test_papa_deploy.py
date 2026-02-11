from testutils import init, is_linux, shell_exec, file_exists, is_windows

# Generic test to verify basic build and deploy functions work
def test_papa_deploy():
    init(__file__, clean_dirs=['bin', 'packages'])

    shell_exec("mama build")
    shell_exec("mama deploy")

    if is_windows():
        extension = '.exe'
    else:
        extension = ''

    assert file_exists(f'bin/ExampleConsumer{extension}'), "Deployed executable not found"

    if is_windows():
        platform_name = 'windows'
    elif is_linux():
        platform_name = 'linux'
    else:
        raise Exception("Unsupported platform")

    assert file_exists(f'packages/ExampleConsumer/{platform_name}/deploy/ExampleConsumer/papa.txt'), "Deployed papa.txt not found for dependency"
