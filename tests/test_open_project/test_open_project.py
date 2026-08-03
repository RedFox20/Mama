"""Pins `mama open`: it finds the IDE project the platform's generator writes, newest format included."""
import os
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from mama.main import _find_ide_project
from mama.platforms.linux import Linux
from mama.platforms.macos import Macos
from mama.platforms.windows import Windows


def _dep(tmp_path, *names, ages=()):
    """A dep whose build dir holds `names`. `ages` sets an mtime per name, oldest last."""
    for i, name in enumerate(names):
        path = tmp_path / name
        if name.endswith('.xcodeproj'): path.mkdir()
        else: path.write_text('')
        if ages: os.utime(path, (ages[i], ages[i]))
    return SimpleNamespace(build_dir=str(tmp_path))


@pytest.mark.parametrize('solution', ['Foo.slnx', 'Foo.sln'])
def test_windows_opens_either_solution_format(tmp_path, solution):
    # VS 18 (2026) with cmake 4.2 writes the XML .slnx, every older toolset writes .sln
    assert _find_ide_project(Windows(Mock()), _dep(tmp_path, solution)).endswith(solution)


def test_the_newest_solution_wins_when_a_dir_holds_both(tmp_path):
    # a dir configured by two toolsets keeps both formats, and the stale one opens an empty solution
    dep = _dep(tmp_path, 'Foo.slnx', 'Foo.sln', ages=(2_000_000_000, 1_000_000_000))
    assert _find_ide_project(Windows(Mock()), dep).endswith('.slnx')


def test_macos_opens_the_xcode_project_dir(tmp_path):
    assert _find_ide_project(Macos(Mock()), _dep(tmp_path, 'Foo.xcodeproj')).endswith('.xcodeproj')


def test_an_empty_build_dir_finds_nothing(tmp_path):
    assert _find_ide_project(Windows(Mock()), _dep(tmp_path)) == ''


def test_a_platform_with_no_ide_project_falls_through_to_vscode(tmp_path):
    assert _find_ide_project(Linux(Mock()), _dep(tmp_path, 'Foo.slnx')) == ''
