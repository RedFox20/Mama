"""Pins the executable suffix in run commands: it follows the HOST as well as the target platform."""
import pytest

from testutils import make_configured_target, set_mock_platform
from mama.utils import run
from mama.platforms.android import Android
from mama.platforms.linux import Linux
from mama.platforms.windows import Windows


def _exe(tmp_path, monkeypatch, host_windows, platform_class, program):
    monkeypatch.setattr(run.System, 'windows', host_windows)
    target, dep = make_configured_target(tmp_path)
    set_mock_platform(dep.config, platform_class)
    _, exe, _ = run.get_cwd_exe_args(target, program, cwd='/proj')
    return exe


@pytest.mark.parametrize('host_windows,platform_class,program,expected', [
    (True,  Windows, 'tools/protoc',     'tools/protoc.exe'),  # an MSVC build needs the suffix
    (True,  Windows, 'tools/protoc.exe', 'tools/protoc.exe'),  # already there, added once
    (True,  Android, 'tools/protoc.exe', 'tools/protoc.exe'),  # a HOST tool inside a cross build
    (False, Android, 'tools/protoc.exe', 'tools/protoc'),      # posix host cannot run a .exe
    (False, Linux,   'bin/app.exe',      'bin/app'),
])
def test_the_suffix_follows_the_host_and_the_target(host_windows, platform_class, program, expected,
                                                    tmp_path, monkeypatch):
    """A cross target has no suffix of its own, but a mamafile still runs HOST tools during that
    build. Deciding on the target alone stripped `.exe` off every host tool on a Windows box."""
    assert _exe(tmp_path, monkeypatch, host_windows, platform_class, program).endswith(expected)
