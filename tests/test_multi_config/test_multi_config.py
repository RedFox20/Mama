"""Pins the configuration set a multi-config generator gets on the cmake command line."""
import pytest

from testutils import configure_cmd as _configure_cmd


@pytest.mark.parametrize('generator, multi', [
    ('-G "Ninja"', False),
    ('-G "Unix Makefiles"', False),
    ('-G "Xcode"', True),
    ('-G "Visual Studio 17 2022" -A x64', True),
    ('-G "Ninja Multi-Config"', True),
])
def test_only_a_multi_config_generator_names_the_configuration_set(tmp_path, generator, multi):
    cmd = _configure_cmd(tmp_path, generator)
    assert '-DCMAKE_BUILD_TYPE=RelWithDebInfo' in cmd
    assert ('-DCMAKE_CONFIGURATION_TYPES=' in cmd) is multi


@pytest.mark.parametrize('debug, expect', [
    (False, 'RelWithDebInfo;Debug'),
    (True,  'Debug;RelWithDebInfo'),
])
def test_the_configuration_set_names_the_type_this_target_builds_first(tmp_path, debug, expect):
    cmd = _configure_cmd(tmp_path, '-G "Xcode"', debug=debug, release=not debug)
    assert f'-DCMAKE_CONFIGURATION_TYPES="{expect}"' in cmd
