"""Pins that every `mama install-<tool>` reaches its commands with arguments they accept.

autospec binds the real signature of execute and execute_piped, so a call naming a keyword
the function does not have raises TypeError here. A plain Mock swallows any keyword, which
is why install_gcc shipped `execute(..., exit_on_fail=False)` and failed in a consumer CI.
"""
from unittest.mock import patch

import pytest

from mama.build_config import BuildConfig


# every tool string run_convenient_installs dispatches on, and a substring only that
# installer produces, so a dispatch that reaches the wrong branch fails the case
TOOLS = [('gcc-14', 'g++-14'), ('clang-21', 'clang-tidy-21'),
         ('raspi-arm64', 'aarch64-linux-gnu'), ('msbuild', 'dotnet-sdk')]


def _linux_ubuntu(cfg):
    """The installers refuse a non-ubuntu host early, which would skip every command."""
    cfg.distro = ('ubuntu', '24', '04')
    return patch.multiple('mama.build_config.System', windows=False, macos=False, linux=True)


def _run(tool, exists=True):
    """Runs one convenient install with every outbound call mocked, and returns the execute mock."""
    cfg = BuildConfig(['build'])
    cfg.convenient_install = [tool]
    with _linux_ubuntu(cfg), \
         patch('mama.build_config.execute', autospec=True, return_value=0) as ex, \
         patch('mama.build_config.execute_piped', autospec=True, return_value='14.2.0'), \
         patch('mama.build_config.console'), patch('mama.build_config.warning'), \
         patch('mama.build_config.distro') as dist, \
         patch('mama.build_config.os.path.exists', return_value=exists):
        dist.info.return_value = {'codename': 'noble'}
        cfg.run_convenient_installs()
    return ex


@pytest.mark.parametrize('tool,marker', TOOLS)
def test_install_reaches_its_commands_with_arguments_they_accept(tool, marker):
    ex = _run(tool)
    commands = [call.args[0] if call.args else '' for call in ex.call_args_list]
    assert commands, f'{tool} ran no command, so the mocks skipped the installer'
    assert any(marker in str(cmd) for cmd in commands), \
           f'{tool} ran {len(commands)} commands and none named {marker}: {commands}'


def test_a_failing_apt_update_warns_and_keeps_going():
    """install_gcc reads the status code instead of raising, so a blocked PPA is not fatal."""
    cfg = BuildConfig(['build'])
    cfg.convenient_install = ['gcc-14']
    with _linux_ubuntu(cfg), \
         patch('mama.build_config.execute', autospec=True) as ex, \
         patch('mama.build_config.console'), patch('mama.build_config.warning') as warn:
        ex.side_effect = lambda cmd, *a, **kw: 1 if 'apt-get update' in cmd else 0
        cfg.run_convenient_installs()
    assert warn.called, 'a failed apt-get update must warn'
    assert any('update-alternatives' in str(c.args[0]) for c in ex.call_args_list), \
           'the install stopped at the failed update instead of continuing'
