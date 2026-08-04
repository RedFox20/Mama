"""Pins resolve_executable: it finds the program, memoizes the PATH scan and reports a miss as ''."""
import os
import pytest

from mama.utils.sub_process import resolve_executable
from testutils import is_windows


@pytest.fixture(autouse=True)
def _fresh_cache():
    resolve_executable.cache_clear()


def test_a_program_on_the_path_resolves_to_its_absolute_path():
    found = resolve_executable('git', os.getcwd())
    assert os.path.isabs(found) and os.path.isfile(found)


def test_the_path_scan_runs_once_per_name_and_dir():
    # shutil.which reads every PATH dir and costs about 2ms for cmake, and a build spawns hundreds of children
    cwd = os.getcwd()
    for _ in range(5): resolve_executable('git', cwd)
    assert resolve_executable.cache_info().misses == 1


def test_a_missing_program_answers_empty_rather_than_raising():
    assert resolve_executable('mama_no_such_program_here', os.getcwd()) == ''


def test_a_file_in_the_working_dir_wins_over_the_path(tmp_path, monkeypatch):
    # the cwd belongs in the cache key: a relative name, and a bare name on Windows, resolve there first
    exe = tmp_path / ('tool.exe' if is_windows() else 'tool')
    exe.write_text('')
    monkeypatch.chdir(tmp_path)
    assert resolve_executable('tool', str(tmp_path)) == os.path.abspath(str(exe))
