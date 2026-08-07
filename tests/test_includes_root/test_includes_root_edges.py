"""Pins the as_includes_root edges: one root per target, the overlap compare, the filter it carries,
and what a reload of the deployed tree does."""
import os
from unittest.mock import patch
import pytest

import mama.package as package
from mama.papa_deploy import _append_includes
from test_includes_root import make_mock_target, make_mock_dep


@pytest.fixture
def lib(tmp_path):
    """tmp/src/mylib/*.h plus tmp/src/other/*.h, so a test can export one or both."""
    for sub, names in (('mylib', ('mylib.h', 'detail.inc')), ('other', ('other.h',))):
        d = tmp_path / 'src' / sub
        d.mkdir(parents=True)
        for n in names: (d / n).write_text('#pragma once\n')
    target = make_mock_target(str(tmp_path))
    target.dep = make_mock_dep(target)
    return target


def _export(target, path, **kw):
    return package.export_include(target, path, build_dir=False, **kw)


def test_a_second_includes_root_replaces_the_first(lib):
    # only one root exists per target, so the LAST call decides where every header lands
    _export(lib, 'src/mylib', as_includes_root='mylib')
    first = lib.includes_root
    _export(lib, 'src/other', as_includes_root='other')
    assert lib.includes_root != first
    assert lib.includes_root[2] == 'other'


def test_the_overlap_check_compares_against_the_shipped_dir_not_the_parent(lib):
    # the root export records the PARENT, so a plain export of a sibling must not read as an overlap
    _export(lib, 'src/mylib', as_includes_root='mylib')
    with patch('mama.package.warning') as warn:
        assert _export(lib, 'src/other') is True
    assert not [c for c in warn.call_args_list if 'overlaps' in c[0][0]]
    assert len(lib.exported_includes) == 2


def test_an_alias_that_matches_the_dir_name_still_re_roots(lib):
    # as_includes_root='mylib' over src/mylib is a rename to the same name, and must stay a rename
    _export(lib, 'src/mylib', as_includes_root='mylib')
    root_path, root_src, alias = lib.includes_root
    assert alias == 'mylib'
    assert root_src.endswith('/src/mylib') and root_path.endswith('/src')


def test_a_bool_root_and_a_matching_string_root_agree(tmp_path):
    """as_includes_root=True takes the basename, so it must equal the explicit spelling."""
    trees = []
    for i, arg in enumerate((True, 'mylib')):
        d = tmp_path / str(i) / 'src' / 'mylib'
        d.mkdir(parents=True)
        (d / 'mylib.h').write_text('#pragma once\n')
        t = make_mock_target(str(tmp_path / str(i)))
        t.dep = make_mock_dep(t)
        _export(t, 'src/mylib', as_includes_root=arg)
        trees.append((os.path.basename(t.includes_root[1]), t.includes_root[2]))
    assert trees[0] == trees[1]


def test_a_missing_root_path_leaves_the_target_unrooted(lib):
    assert _export(lib, 'src/nope', as_includes_root='nope') is False
    assert lib.includes_root == ('', '', '')  # a failed export must not half-set the root


def test_the_filter_of_a_root_export_applies_to_the_whole_target(lib, tmp_path):
    # includes_filter is target state, so it decides what a LATER export ships too
    lib.include_glob_filter = ['.h', '.inc']
    _export(lib, 'src/mylib', as_includes_root='mylib')
    pkg = str(tmp_path / 'deploy')
    descr = []
    _append_includes(lib, pkg, False, descr, [(lib.dep, p) for p in lib.exported_includes])
    shipped = sorted(os.listdir(os.path.join(pkg, 'include', 'mylib')))
    assert shipped == ['detail.inc', 'mylib.h']


def test_a_root_export_writes_one_include_record(lib, tmp_path):
    _export(lib, 'src/mylib', as_includes_root='mylib')
    descr = []
    _append_includes(lib, str(tmp_path / 'deploy'), False, descr, [(lib.dep, p) for p in lib.exported_includes])
    assert descr == ['I include']  # the consumer adds `include`, then writes #include <mylib/mylib.h>


def test_re_running_the_same_export_keeps_one_entry(lib):
    # package() runs again on every build of a fetched dep, so a repeat must not grow the export list
    _export(lib, 'src/mylib', as_includes_root='mylib')
    before = list(lib.exported_includes)
    _export(lib, 'src/mylib', as_includes_root='mylib')
    assert lib.exported_includes == before


def test_the_deploy_reports_how_many_header_files_it_shipped(lib, tmp_path):
    # one record names a whole dir, so the record count alone never says how much a package ships
    lib.include_glob_filter = ['.h', '.inc']
    _export(lib, 'src/mylib')
    _export(lib, 'src/other')
    descr = []
    shipped = _append_includes(lib, str(tmp_path / 'deploy'), False, descr,
                               [(lib.dep, p) for p in lib.exported_includes])
    assert descr == ['I include/mylib', 'I include/other']
    assert shipped == 3  # mylib.h, detail.inc, other.h


def test_an_export_of_nothing_ships_no_files(lib, tmp_path):
    assert _append_includes(lib, str(tmp_path / 'deploy'), False, [], []) == 0
