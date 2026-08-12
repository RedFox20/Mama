"""Pins what a failed cmake build reports when the child died on a signal and left no output."""
import pytest
from unittest.mock import patch
from mama.buildsys.cmake import configure as cc
from mama.utils.errors import BuildError
from testutils import is_windows, make_configured_target


def _build_fails(tmp_path, status, output) -> str:
    target, _ = make_configured_target(tmp_path)
    with patch('mama.buildsys.cmake.configure.execute_piped_echo', return_value=(status, output)), \
         pytest.raises(BuildError) as err:
        cc.run_build(target, install=False)
    return str(err.value)


def test_a_killed_compiler_names_the_signal_and_the_silence(tmp_path):
    msg = _build_fails(tmp_path, -9 if not is_windows() else -11, '')
    assert 'killed by SIG' in msg and 'printed no output' in msg


def test_a_compiler_that_reported_its_own_error_gets_no_silence_note(tmp_path):
    msg = _build_fails(tmp_path, 1, 'error: no member named foo')
    assert 'exit code 1' in msg and 'printed no output' not in msg
