"""Pins the export_modules() recording rules and the two helpers a deploy and the cmake writer share."""
from unittest.mock import patch

from testutils import make_includes_target, write_files

from mama import package


MODULES = {'src/rpp/rpp-strview.cppm': 'export module rpp.strview;',
           'src/rpp/rpp-debugging.ixx': 'export module rpp.debugging;',
           'src/rpp/strview.h': '#pragma once'}


def _target(tmp_path, files=None):
    write_files(tmp_path, files if files is not None else MODULES)
    return make_includes_target(str(tmp_path))


def test_a_glob_records_every_module_extension_and_no_header(tmp_path):
    target = _target(tmp_path)
    assert package.export_modules(target, 'src/rpp', None, build_dir=False)
    assert [m.rsplit('/', 1)[1] for m in target.exported_modules] \
        == ['rpp-debugging.ixx', 'rpp-strview.cppm']  # sorted, so the record order never drifts


def test_an_explicit_list_records_exactly_those_files_in_order(tmp_path):
    target = _target(tmp_path)
    package.export_modules(target, 'src/rpp', ['rpp-strview.cppm', 'rpp-debugging.ixx'], build_dir=False)
    assert [m.rsplit('/', 1)[1] for m in target.exported_modules] == ['rpp-strview.cppm', 'rpp-debugging.ixx']


def test_a_missing_module_warns_and_records_nothing(tmp_path):
    target = _target(tmp_path)
    with patch('mama.package.warning') as warned:
        assert not package.export_modules(target, 'src/rpp', ['nope.cppm'], build_dir=False)
    assert target.exported_modules == []
    assert 'nope.cppm' in str(warned.call_args)


def test_recording_the_same_module_twice_keeps_one_entry(tmp_path):
    target = _target(tmp_path)
    package.export_modules(target, 'src/rpp', ['rpp-strview.cppm'], build_dir=False)
    package.export_modules(target, 'src/rpp', ['rpp-strview.cppm'], build_dir=False)
    assert len(target.exported_modules) == 1


def test_module_suffixes_are_distinct_and_empty_without_modules(tmp_path):
    target = _target(tmp_path)
    assert package.module_suffixes(target) == ()
    package.export_modules(target, 'src/rpp', None, build_dir=False)
    assert sorted(package.module_suffixes(target)) == ['.cppm', '.ixx']


def test_module_base_dir_picks_the_longest_matching_export(tmp_path):
    target = _target(tmp_path)
    package.export_modules(target, 'src/rpp', ['rpp-strview.cppm'], build_dir=False)
    module = target.exported_modules[0]
    target.exported_includes = [f'{tmp_path}/src', f'{tmp_path}/src/rpp']
    assert package.module_base_dir(target, module) == f'{tmp_path}/src/rpp'


def test_module_base_dir_is_empty_when_no_export_holds_the_module(tmp_path):
    target = _target(tmp_path)
    package.export_modules(target, 'src/rpp', ['rpp-strview.cppm'], build_dir=False)
    target.exported_includes = [f'{tmp_path}/include']
    assert package.module_base_dir(target, target.exported_modules[0]) == ''
