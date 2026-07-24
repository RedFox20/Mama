"""Pins the shared ProgressBar throttle and PAPA archive entry collection + progress reporting."""
import os
import zipfile
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from mama.utils import system
from mama.util import ProgressBar
from mama.papa_upload import (_archive_entries, _write_archive, _write_file, _compress_level,
                              _archive_total_size)


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
    assert sizes['include'] is None and sizes['include/sub'] is None  # dirs have no payload to stream


def test_archive_entries_handles_a_plain_file(tmp_path):
    lib = tmp_path / 'lib.a'
    lib.write_text('xyz')
    assert _archive_entries('lib/lib.a', str(lib)) == [(str(lib), 'lib/lib.a', 3)]


def _archive(tmp_path, **cfg):
    lib = tmp_path / 'libsample.a'
    lib.write_text('x' * 32)
    groups = [('      adding lib/libsample.a', _archive_entries('lib/libsample.a', str(lib)))]
    with zipfile.ZipFile(tmp_path / 'out.zip', 'w') as zip:
        _write_archive(zip, groups, SimpleNamespace(**cfg), '  - sample  ', _archive_total_size(groups))


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


def test_the_bar_names_the_file_currently_being_archived(capsys):
    bar = ProgressBar(100*1024*1024)
    capsys.readouterr()
    bar.step(50*1024*1024, 'lib/libprotobuf.a')
    assert 'lib/libprotobuf.a' in capsys.readouterr().out
    bar.finish()
    assert 'libprotobuf.a' not in capsys.readouterr().out  # nothing is in flight at 100%


def test_a_long_path_is_truncated_from_the_left(capsys):
    bar = ProgressBar(100*1024*1024)
    capsys.readouterr()
    bar.step(50*1024*1024, 'lib/' + 'deep/'*20 + 'libabsl_log_internal_structured_proto.a')
    out = capsys.readouterr().out
    assert 'structured_proto.a' in out and max(len(l) for l in out.split('\r')) < 130


def test_compression_drops_to_6_only_for_big_packages():
    assert _compress_level(50*1024*1024) == 8
    assert _compress_level(100*1024*1024) == 8   # at the threshold level 8 is still cheap enough
    assert _compress_level(101*1024*1024) == 6


def test_archive_total_size_ignores_dir_entries(tmp_path):
    root = tmp_path / 'include'
    root.mkdir()
    (root / 'a.h').write_text('aaaa')
    assert _archive_total_size([('', _archive_entries('include', str(root)))]) == 4


def test_a_large_file_advances_the_bar_while_it_is_written(tmp_path):
    # the whole point: a 60MB lib used to leave the bar frozen, then jump
    big = tmp_path / 'libbig.a'
    big.write_bytes(b'x' * (3*1024*1024 + 7))
    bar = Mock()
    with zipfile.ZipFile(tmp_path / 'o.zip', 'w', compression=zipfile.ZIP_DEFLATED) as zip:
        _write_file(zip, str(big), 'lib/libbig.a', bar)
    assert bar.step.call_count == 4  # 3 whole chunks + the remainder
    assert all(c.args[1] == 'lib/libbig.a' for c in bar.step.call_args_list)


def test_streamed_write_still_compresses_and_round_trips(tmp_path):
    src = tmp_path / 'lib.a'
    body = b'A' * (256*1024)
    src.write_bytes(body)
    with zipfile.ZipFile(tmp_path / 'o.zip', 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=8) as zip:
        _write_file(zip, str(src), 'lib/lib.a', None)
    with zipfile.ZipFile(tmp_path / 'o.zip') as zip:
        info = zip.getinfo('lib/lib.a')
        assert info.compress_type == zipfile.ZIP_DEFLATED  # not silently stored
        assert info.compress_size < info.file_size // 10
        assert zip.read('lib/lib.a') == body


def test_streamed_write_records_the_same_metadata_as_zipfile_write(tmp_path):
    # bin/protoc must stay executable after a round trip, so the streamed path has to match write()
    src = tmp_path / 'tool'
    src.write_bytes(b'#!/bin/sh\n')
    os.chmod(src, 0o755)
    with zipfile.ZipFile(tmp_path / 'a.zip', 'w') as zip: zip.write(str(src), 'bin/tool')
    with zipfile.ZipFile(tmp_path / 'b.zip', 'w') as zip: _write_file(zip, str(src), 'bin/tool', None)
    written = zipfile.ZipFile(tmp_path / 'a.zip').getinfo('bin/tool')
    streamed = zipfile.ZipFile(tmp_path / 'b.zip').getinfo('bin/tool')
    assert (streamed.external_attr, streamed.date_time) == (written.external_attr, written.date_time)
