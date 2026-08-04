"""Pins MSVC toolset selection: newest version with a live cl.exe, not os.listdir order."""
from unittest.mock import Mock

from mama.platforms import windows
from mama.platforms.windows import latest_msvc_toolset

def _toolset(root, ver, with_cl=True):
    d = root / ver / 'bin' / 'Hostx64' / 'x64'; d.mkdir(parents=True)
    if with_cl: (d / 'cl.exe').write_text('')


def test_picks_newest_version_numerically(tmp_path):
    for v in ('14.44.35207', '14.51.36231', '14.9.0'):   # 14.51 > 14.9 numerically (lexically 14.9 would win)
        _toolset(tmp_path, v)
    assert latest_msvc_toolset(str(tmp_path)).endswith('14.51.36231')


def test_skips_newest_when_its_cl_was_removed_by_an_upgrade(tmp_path):
    _toolset(tmp_path, '14.51.36231', with_cl=False)   # dir left behind without binaries
    _toolset(tmp_path, '14.44.35207')
    assert latest_msvc_toolset(str(tmp_path)).endswith('14.44.35207')


def test_empty_or_missing_root_returns_empty(tmp_path):
    assert latest_msvc_toolset(str(tmp_path / 'nope')) == ''
    (tmp_path / 'empty').mkdir()
    assert latest_msvc_toolset(str(tmp_path / 'empty')) == ''


def test_the_visual_studio_detection_prints_once(tmp_path, monkeypatch, capsys):
    # mama memoizes the path, so a print outside the memo wrote the same line once per caller
    monkeypatch.setattr(windows, '_found', {})
    monkeypatch.setattr(windows, 'vswhere_property', lambda name: str(tmp_path))
    monkeypatch.setattr(windows.System, 'windows', True)
    for _ in range(3): windows.find_visualstudio(verbose=True)
    assert capsys.readouterr().out.count('Detected VisualStudio') == 1


def test_the_msvc_tools_detection_prints_once(tmp_path, monkeypatch, capsys):
    _toolset(tmp_path, '14.51.36231')
    monkeypatch.setattr(windows, '_found', {})
    monkeypatch.setattr(windows.Windows, 'visualstudio_path', lambda self: str(tmp_path))
    monkeypatch.setattr(windows, 'latest_msvc_toolset', lambda root: str(tmp_path / '14.51.36231'))
    platform = windows.Windows(Mock(verbose=True))
    for _ in range(3): platform.msvc_tools_path()
    assert capsys.readouterr().out.count('Detected MSVC Tools') == 1


def test_toolset_version_keeps_the_major_and_minor_only():
    for path in ('C:/VS/VC/Tools/MSVC/14.51.36231', 'C:/VS/VC/Tools/MSVC/14.51.36231/'):
        assert windows.msvc_toolset_version(path) == '14.51'


def test_the_archive_tag_carries_the_toolset_minor(monkeypatch):
    # 'msvc14' tagged every toolset since 2015 alike, so an upgrade reused the archive of the old one
    monkeypatch.setattr(windows.Windows, 'msvc_tools_path', lambda self: 'C:/VS/VC/Tools/MSVC/14.51.36231')
    assert windows.Windows(Mock(verbose=False)).compiler_version_tag() == 'msvc14.51'  # same shape as gcc14.3


def test_a_toolset_without_cl_is_rejected_not_returned(tmp_path):
    # every msvc_* path is bin/Hostx64/x64, so handing back a dir without cl.exe only moves the
    # failure somewhere more confusing - msvc_tools_path() raises 'Could not detect MSVC Tools'
    (tmp_path / '14.51.36112' / 'bin' / 'Hostx86' / 'x86').mkdir(parents=True)
    assert latest_msvc_toolset(str(tmp_path)) == ''
