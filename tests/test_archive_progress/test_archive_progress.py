"""Pins the shared ProgressBar throttle and PAPA archive entry collection + progress reporting."""
import zipfile
from types import SimpleNamespace

import pytest

from mama.utils import system
from mama.util import ProgressBar
from mama.papa_upload import _archive_entries, _write_archive


@pytest.fixture(autouse=True)
def reset_progress_state():
    yield
    system._progress_active = False  # a bar left mid-redraw makes the next test's console() insert \n


def test_a_small_payload_draws_no_intermediate_redraws(capsys):
    bar = ProgressBar(1000)  # far under the ~1MB floor: redraws would just flicker
    capsys.readouterr()
    for _ in range(10): bar.step(100)
    assert capsys.readouterr().out == ''
    bar.finish()
    assert '100%' in capsys.readouterr().out  # ...but the bar is always committed


def test_a_large_payload_redraws_as_it_advances(capsys):
    total = 100*1024*1024
    bar = ProgressBar(total)
    capsys.readouterr()
    for _ in range(10): bar.step(total // 10)
    assert capsys.readouterr().out.count('%') >= 5


def test_finish_reports_the_real_percent_not_a_fake_100(capsys):
    bar = ProgressBar(1000)
    bar.step(400)
    capsys.readouterr()
    bar.finish()
    assert '40%' in capsys.readouterr().out  # a truncated transfer must stay visible


def test_an_empty_payload_does_not_divide_by_zero(capsys):
    bar = ProgressBar(0)
    bar.step(0)
    bar.finish()
    assert '100%' in capsys.readouterr().out


def test_archive_entries_flattens_a_dir_and_stats_each_file(tmp_path):
    root = tmp_path / 'include'
    (root / 'sub').mkdir(parents=True)
    (root / 'a.h').write_text('aaaa')
    (root / 'sub' / 'b.h').write_text('bb')
    sizes = {rel: size for _, rel, size in _archive_entries('include', str(root))}
    assert sizes['include/a.h'] == 4 and sizes['include/sub/b.h'] == 2
    assert sizes['include'] == 0 and sizes['include/sub'] == 0  # dirs carry no compression weight


def test_archive_entries_handles_a_plain_file(tmp_path):
    lib = tmp_path / 'lib.a'
    lib.write_text('xyz')
    assert _archive_entries('lib/lib.a', str(lib)) == [(str(lib), 'lib/lib.a', 3)]


def _archive(tmp_path, **cfg):
    lib = tmp_path / 'libsample.a'
    lib.write_text('x' * 32)
    groups = [('      adding lib/libsample.a', _archive_entries('lib/libsample.a', str(lib)))]
    with zipfile.ZipFile(tmp_path / 'out.zip', 'w') as zip:
        _write_archive(zip, groups, SimpleNamespace(**cfg), '  - sample  ')


def test_regular_verbosity_shows_a_progress_bar(tmp_path, capsys):
    _archive(tmp_path, print=True, verbose=False)
    out = capsys.readouterr().out
    assert '100%' in out and 'sample' in out and 'adding' not in out


def test_verbose_keeps_per_record_lines_and_no_bar(tmp_path, capsys):
    # a redrawing bar interleaved with scrolling per-record lines corrupts both
    _archive(tmp_path, print=True, verbose=True)
    out = capsys.readouterr().out
    assert 'adding lib/libsample.a' in out and '%' not in out


def test_silent_mode_prints_nothing(tmp_path, capsys):
    _archive(tmp_path, print=False, verbose=False)
    assert capsys.readouterr().out == ''
