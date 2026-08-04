"""Pins the mama.utils.fileio file helpers."""
import os
from mama.utils.fileio import is_file_unmodified


def _pair(tmp_path, a_text, b_text):
    a, b = tmp_path / 'a', tmp_path / 'b'
    a.write_text(a_text); b.write_text(b_text)
    t = os.path.getmtime(a)
    os.utime(b, (t, t))
    return str(a), str(b)


def test_is_file_unmodified_true_for_equal_mtime_and_size(tmp_path):
    assert is_file_unmodified(*_pair(tmp_path, 'xx', 'yy'))


def test_is_file_unmodified_false_on_size_change(tmp_path):
    assert not is_file_unmodified(*_pair(tmp_path, 'xx', 'yyy'))
