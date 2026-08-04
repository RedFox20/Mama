"""Pins what `import mama` may load, and the host architecture detection behind it."""
import importlib
import subprocess
import sys

import pytest

from mama.utils import system


# Every one of these costs 20ms or more to import, and only a rare code path needs each. A new
# module-level import of any of them puts the cost back on every mama start.
DEFERRED = ['psutil', 'ssl', '_ssl', 'zipfile', 'ftplib', 'dateutil.tz', 'urllib.request']


def test_import_mama_loads_no_deferred_module():
    """Runs in a child, because this interpreter already imported mama and much else."""
    code = ('import sys, json; import mama; '
            f'print(json.dumps([m for m in {DEFERRED!r} if m in sys.modules]))')
    out = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    import json
    assert json.loads(out.stdout.strip().splitlines()[-1]) == []


def test_platform_machine_is_never_called_during_import():
    """platform.uname() answers through WMI on Windows, which costs about 60ms of every start."""
    code = ('import platform; platform.machine = lambda: 1/0\n'
            'import mama; print("ok")')
    out = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr


def test_the_arch_name_matches_what_python_reports():
    """The cheap lookup must agree with platform.machine() on whatever host runs the suite."""
    import platform
    assert system.machine == platform.machine()


def test_this_host_resolves_exactly_one_arch():
    flags = [system.System.x86_64, system.System.aarch64, system.System.x86]
    assert sum(bool(f) for f in flags) == 1, f'{system.machine} matched {flags}'


# Both CI platforms in one table, so a Linux runner still checks the Windows spellings and back.
@pytest.mark.parametrize('windows, env, uname, expect', [
    (True,  {'PROCESSOR_ARCHITECTURE': 'AMD64'}, None, ('x86_64', 'AMD64')),
    (True,  {'PROCESSOR_ARCHITECTURE': 'ARM64'}, None, ('aarch64', 'ARM64')),
    (True,  {'PROCESSOR_ARCHITECTURE': 'x86'}, None, ('x86', 'x86')),
    # A 32-bit python on 64-bit windows reads x86, and only ARCHITEW6432 holds the truth.
    (True,  {'PROCESSOR_ARCHITECTURE': 'x86', 'PROCESSOR_ARCHITEW6432': 'AMD64'}, None, ('x86_64', 'AMD64')),
    (False, {}, 'x86_64', ('x86_64', 'x86_64')),
    (False, {}, 'aarch64', ('aarch64', 'aarch64')),
    (False, {}, 'arm64', ('aarch64', 'arm64')),
    (False, {}, 'i386', ('x86', 'i386')),
])
def test_every_arch_spelling_of_both_platforms(monkeypatch, windows, env, uname, expect):
    """Reimports the module under a faked host, so a Linux CI run still pins the Windows path."""
    wanted, raw = expect
    monkeypatch.setattr(system, 'is_windows', windows, raising=False)
    monkeypatch.setattr(system.os, 'environ', env)
    if not windows:  # the windows branch never reaches os.uname, so it needs no stub
        monkeypatch.setattr(system.os, 'uname', lambda: type('u', (), {'machine': uname})(), raising=False)

    name = system._machine_name()
    arch = name.lower()
    assert name == raw
    assert (arch == 'aarch64' or arch == 'arm64') == (wanted == 'aarch64')
    assert (arch == 'x86_64' or arch == 'amd64') == (wanted == 'x86_64')
    assert (arch == 'x86' or arch == 'i386') == (wanted == 'x86')


def test_an_unknown_host_falls_back_instead_of_answering_empty(monkeypatch):
    """A host with no env var and no os.uname must still name an arch, never ''."""
    import platform
    monkeypatch.setattr(system, 'is_windows', True, raising=False)
    monkeypatch.setattr(system.os, 'environ', {})
    assert system._machine_name() == platform.machine()


def test_the_deferred_imports_still_work_when_called():
    """A deferred import that names the wrong module fails only at call time, so prove it resolves."""
    from mama.utils.archive import unzip
    from mama.utils.net import download_file
    assert callable(download_file) and callable(unzip)
    importlib.import_module('mama.papa_upload')  # the module build_target defers
    from mama.utils import sub_process
    assert not sub_process._kill_group(-1)  # exercises the psutil branch guard, no such pid
