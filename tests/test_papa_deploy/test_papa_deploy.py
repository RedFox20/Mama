"""Integration: pins that build + deploy produce the consumer executable and its papa.txt."""
from testutils import init, mama_exec, file_exists, executable_extension, native_platform_name


def test_papa_deploy(tmp_path, buildable_example_remote):
    init(__file__, tmp_path)

    mama_exec(['build'])
    mama_exec(['deploy'])

    assert file_exists(f'bin/ExampleConsumer{executable_extension()}'), "Deployed executable not found"
    assert file_exists(f'packages/ExampleConsumer/{native_platform_name()}/deploy/ExampleConsumer/papa.txt'), "Deployed papa.txt not found for dependency"
