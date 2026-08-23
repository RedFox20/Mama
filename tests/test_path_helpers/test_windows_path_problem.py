"""Pins the portability check the conftest fixture runs over every tmp_path a test writes."""
import pytest

from testutils import windows_path_problem


@pytest.mark.parametrize('path', [
    'plain/path.txt',
    'ok/src)dir/f.txt',                 # a paren is legal, and a cmake parser fixture writes one
    'ok/${CMAKE_SOURCE_DIR}/src/f',     # so are the braces of an unexpanded variable
    'le$af/x',
    'src dir/CMakeLists.txt',           # a space inside a name is legal, only a trailing one is not
    'C:/abs/path',                      # the colon of a drive letter names no component
])
def test_a_portable_path_reports_nothing(path):
    assert windows_path_problem(path) == ''


@pytest.mark.parametrize('path, holds', [
    ('src\tdir/CMakeLists.txt', 'holds'),   # every character below 0x20 is refused
    ('a:b/x', 'holds'),
    ('a<b/x', 'holds'),
    ('a|b/x', 'holds'),
    ('name /x', 'ends in'),                 # Windows drops a trailing space
    ('name./x', 'ends in'),
    ('con/x.txt', 'reserved'),              # a device name is reserved whatever follows it
    ('src/aux.cpp', 'reserved'),
    ('src/COM1.txt', 'reserved'),           # and the match folds case
])
def test_a_path_windows_refuses_names_the_reason(path, holds):
    assert holds in windows_path_problem(path)
