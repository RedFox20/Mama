"""Pins MSVC toolset selection: newest version with a live cl.exe, not os.listdir order."""
from mama.build_config import BuildConfig

def _toolset(root, ver, with_cl=True):
    d = root / ver / 'bin' / 'Hostx64' / 'x64'; d.mkdir(parents=True)
    if with_cl: (d / 'cl.exe').write_text('')


def test_picks_newest_version_numerically(tmp_path):
    for v in ('14.44.35207', '14.51.36231', '14.9.0'):   # 14.51 > 14.9 numerically (lexically 14.9 would win)
        _toolset(tmp_path, v)
    assert BuildConfig._latest_msvc_toolset(str(tmp_path)).endswith('14.51.36231')


def test_skips_newest_when_its_cl_was_removed_by_an_upgrade(tmp_path):
    _toolset(tmp_path, '14.51.36231', with_cl=False)   # dir left behind without binaries
    _toolset(tmp_path, '14.44.35207')
    assert BuildConfig._latest_msvc_toolset(str(tmp_path)).endswith('14.44.35207')


def test_empty_or_missing_root_returns_empty(tmp_path):
    assert BuildConfig._latest_msvc_toolset(str(tmp_path / 'nope')) == ''
    (tmp_path / 'empty').mkdir()
    assert BuildConfig._latest_msvc_toolset(str(tmp_path / 'empty')) == ''


def test_a_toolset_without_cl_is_rejected_not_returned(tmp_path):
    # every get_msvc_* path is bin/Hostx64/x64, so handing back a dir without cl.exe only moves the
    # failure somewhere more confusing - get_msvc_tools_path() raises 'Could not detect MSVC Tools'
    (tmp_path / '14.51.36112' / 'bin' / 'Hostx86' / 'x86').mkdir(parents=True)
    assert BuildConfig._latest_msvc_toolset(str(tmp_path)) == ''
