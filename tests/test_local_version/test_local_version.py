"""Pins the computed version of a local module: what changes it, what does not, and what refuses to publish."""
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from testutils import make_git_root_with_local_pkgs, make_mock_local_dep
from mama import local_version, util
from mama.mamafile_version import computed_local_version


def _dep(tmp_path):
    return make_git_root_with_local_pkgs(tmp_path)[0]


def _version(dep):
    return local_version.compute_version(dep)


def test_the_same_tree_always_names_the_same_version(tmp_path):
    dep = _dep(tmp_path)
    assert _version(dep) == _version(dep)
    assert len(_version(dep)) == 16


def test_an_edit_to_a_source_file_changes_the_version(tmp_path):
    dep = _dep(tmp_path)
    before = _version(dep)
    (Path(dep.src_dir) / 'lib.cpp').write_text('int f0(){ return 42; }\n')
    assert _version(dep) != before


def test_a_new_file_changes_the_version(tmp_path):
    dep = _dep(tmp_path)
    before = _version(dep)
    (Path(dep.src_dir) / 'extra.h').write_text('#pragma once\n')
    assert _version(dep) != before


def test_a_rename_changes_the_version(tmp_path):
    """The path is hashed next to the content, so moving a file is not the same tree."""
    dep = _dep(tmp_path)
    before = _version(dep)
    (Path(dep.src_dir) / 'lib.cpp').rename(Path(dep.src_dir) / 'renamed.cpp')
    assert _version(dep) != before


@pytest.mark.parametrize('rel_path', ['packages/x/lib.a', 'build/out.o', '.git/HEAD', '__pycache__/m.pyc'])
def test_build_output_never_changes_the_version(tmp_path, rel_path):
    dep = _dep(tmp_path)
    dep.workspace = 'packages'
    dep.build_dir_name = 'build'
    before = _version(dep)
    noise = Path(dep.src_dir) / rel_path
    noise.parent.mkdir(parents=True, exist_ok=True)
    noise.write_text('junk\n')
    assert _version(dep) == before


def test_an_object_file_next_to_the_source_is_ignored(tmp_path):
    dep = _dep(tmp_path)
    before = _version(dep)
    (Path(dep.src_dir) / 'lib.obj').write_text('binary junk\n')
    assert _version(dep) == before


def test_line_endings_do_not_change_the_version(tmp_path):
    """A checkout with core.autocrlf=true writes CRLF. That machine must name the source the same."""
    dep = _dep(tmp_path)
    src = Path(dep.src_dir) / 'lib.cpp'
    src.write_bytes(b'int f(){\n  return 1;\n}\n')
    lf = _version(dep)
    util._git_fingerprints.clear()
    src.write_bytes(b'int f(){\r\n  return 1;\r\n}\r\n')
    assert _version(dep) == lf


def test_a_binary_file_keeps_its_bytes(tmp_path):
    """The CRLF fold must not touch a binary, where 0x0d0a is data and not a line ending."""
    dep = _dep(tmp_path)
    blob = Path(dep.src_dir) / 'table.bin'
    blob.write_bytes(b'\0\x01\x0d\x0a\x02')
    before = _version(dep)
    blob.write_bytes(b'\0\x01\x0a\x02')
    assert _version(dep) != before


def test_the_memo_stops_the_second_run_from_reading_files(tmp_path):
    dep = _dep(tmp_path)
    first = _version(dep)
    real_open = open
    with patch('builtins.open', side_effect=real_open) as opened:
        second = _version(dep)
    reads = [c for c in opened.call_args_list if 'rb' in str(c)]
    assert second == first
    assert not reads  # every file answered from the memo


def test_update_drops_the_memo(tmp_path):
    """A fetch can write new content under an old mtime, so `mama update` must not trust the memo."""
    dep = _dep(tmp_path)
    _version(dep)
    dep.config.update = True
    assert local_version._load_memo(dep) == {}


def test_a_clean_module_may_publish_and_a_dirty_one_may_not(tmp_path):
    dep = _dep(tmp_path)
    util.load_repo_status(str(Path(dep.src_dir).parent.parent))
    assert local_version.is_publishable(dep)
    (Path(dep.src_dir) / 'lib.cpp').write_text('int f0(){ return 7; }\n')
    util.forget_repo_status()
    util._git_fingerprints.clear()
    assert not local_version.is_publishable(dep)


def test_only_a_local_dep_gets_a_computed_version(tmp_path):
    dep = _dep(tmp_path)
    assert computed_local_version(dep)
    dep.is_root = True
    assert computed_local_version(dep) == ''


def test_a_git_dep_gets_no_computed_version(tmp_path):
    """A git dep keeps the text-scan rule, because no reader can walk a tree it has not cloned."""
    dep = _dep(tmp_path)
    dep.dep_source = Mock(is_src=False)
    assert computed_local_version(dep) == ''


def test_a_file_that_vanishes_mid_walk_names_nothing(tmp_path):
    dep = _dep(tmp_path)
    with patch('mama.local_version.os.stat', side_effect=OSError):
        assert local_version.compute_version(dep) == local_version.compute_version(_dep(tmp_path / 'b'))
