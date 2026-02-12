from testutils import init, shell_exec, file_exists, executable_extension, native_platform_name

# Generic test to verify basic build and deploy functions work
def test_papa_deploy():
    init(__file__, clean_dirs=['bin', 'packages'])

    shell_exec("mama build")
    shell_exec("mama deploy")

    assert file_exists(f'bin/ExampleConsumer{executable_extension()}'), "Deployed executable not found"
    assert file_exists(f'packages/ExampleConsumer/{native_platform_name()}/deploy/ExampleConsumer/papa.txt'), "Deployed papa.txt not found for dependency"
