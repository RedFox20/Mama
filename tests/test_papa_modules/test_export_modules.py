"""Pins the export_modules() recording rules and the two helpers a deploy and the cmake writer share."""
from unittest.mock import patch

import pytest

from testutils import make_includes_target, write_files

from mama import package
from mama.utils.paths import normalized_join


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


@pytest.mark.parametrize('recursive, holds_sub', [(True, True), (False, False)])
def test_a_glob_reads_a_subdirectory_only_when_it_recurses(tmp_path, recursive, holds_sub):
    target = _target(tmp_path, dict(MODULES, **{'src/rpp/sub/rpp-sub.cppm': 'export module rpp.sub;'}))
    assert package.export_modules(target, 'src/rpp', None, build_dir=False, recursive=recursive)
    names = [m.rsplit('/', 1)[1] for m in target.exported_modules]
    assert ('rpp-sub.cppm' in names) == holds_sub
    assert 'rpp-strview.cppm' in names  # the named dir answers either way


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
    assert package.module_suffixes([]) == ()
    package.export_modules(target, 'src/rpp', None, build_dir=False)
    assert sorted(package.module_suffixes(target.exported_modules)) == ['.cppm', '.ixx']


def test_one_opt_out_keeps_the_module_objects_whatever_a_later_call_passes(tmp_path):
    # the flag is target-wide, so a second export taking the default must not re-arm the strip
    target = _target(tmp_path)
    target.strip_module_objects = True
    from mama.build_target import BuildTarget
    BuildTarget.export_modules(target, 'src/rpp', ['rpp-strview.cppm'], strip_objects=False)
    BuildTarget.export_modules(target, 'src/rpp', ['rpp-debugging.ixx'])
    assert target.strip_module_objects is False


def test_a_removed_floor_argument_warns_and_exports_anyway(tmp_path):
    # a recipe written against the old API must not fail the run over an argument that does nothing
    target = _target(tmp_path)
    from mama.build_target import BuildTarget
    with patch('mama.build_target.warning') as warn:
        BuildTarget.export_modules(target, 'src/rpp', ['rpp-strview.cppm'], min_clang='18')
    assert 'min_clang' in warn.call_args[0][0]
    assert len(target.exported_modules) == 1


def test_a_macos_casing_variant_still_finds_its_include_dir(tmp_path):
    # one dir on a case-insensitive filesystem, so export_include('Src') holds a module under 'src'
    target = _target(tmp_path)
    package.export_modules(target, 'src/rpp', ['rpp-strview.cppm'], build_dir=False)
    target.exported_includes = [normalized_join(str(tmp_path), 'Src/Rpp')]
    with patch.object(package.System, 'macos', True):
        assert package.module_base_dir(target, target.exported_modules[0])


def test_module_base_dir_picks_the_longest_matching_export(tmp_path):
    target = _target(tmp_path)
    package.export_modules(target, 'src/rpp', ['rpp-strview.cppm'], build_dir=False)
    module = target.exported_modules[0]
    target.exported_includes = [normalized_join(str(tmp_path), 'src'),
                                normalized_join(str(tmp_path), 'src/rpp')]
    assert package.module_base_dir(target, module) == normalized_join(str(tmp_path), 'src/rpp')


def test_module_base_dir_reads_a_backslash_export_too(tmp_path):
    # a mixed spelling used to match nothing, and the deploy then dropped the module with a warning
    target = _target(tmp_path)
    package.export_modules(target, 'src/rpp', ['rpp-strview.cppm'], build_dir=False)
    backslash = normalized_join(str(tmp_path), 'src/rpp').replace('/', '\\')
    target.exported_includes = [backslash]
    assert package.module_base_dir(target, target.exported_modules[0]) == backslash


def test_module_base_dir_is_empty_when_no_export_holds_the_module(tmp_path):
    target = _target(tmp_path)
    package.export_modules(target, 'src/rpp', ['rpp-strview.cppm'], build_dir=False)
    target.exported_includes = [normalized_join(str(tmp_path), 'include')]
    assert package.module_base_dir(target, target.exported_modules[0]) == ''
