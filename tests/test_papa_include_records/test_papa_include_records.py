"""Pins how the PAPA archive treats overlapping include records and duplicated header trees."""
import os, zipfile

import pytest
from testutils import archive_papa_package, deploy_and_archive, make_exporting_target, make_mock_dep

TASK_H = '#pragma once\n// task\n'


def _write_files(root, files:dict):
    for rel, text in files.items():
        os.makedirs(os.path.dirname(f'{root}/{rel}'), exist_ok=True)
        open(f'{root}/{rel}', 'w').write(text)


def _deployed(tmp_path, files:dict, records:list) -> str:
    """A package dir this test writes by hand. papa_deploy never emits an include record outside include/."""
    package = str(tmp_path / 'deploy')
    _write_files(package, files)
    open(f'{package}/papa.txt', 'w').write('\n'.join(['P libfoo'] + records))
    return package


def _built(tmp_path, files:dict, includes:list):
    """A producer build dir with `files` and one lib, plus the target that exports `includes`."""
    dep = make_mock_dep(tmp_path / 'producer', name='libfoo')
    build = dep.build_dir
    _write_files(build, files | {'lib/libfoo.a': '\0'})
    return build, make_exporting_target(dep, [f'{build}/{i}' for i in includes], [f'{build}/lib/libfoo.a'])


# --- an include record inside another one names the same files, so the archive writes them once ---

def test_a_nested_include_record_ships_every_file_once(tmp_path):
    # the qcoro shape: export_include('include') and export_include('include/foo') name the same headers
    build, target = _built(tmp_path, {'include/foo/foo.h': '#pragma once\n', 'include/foo/impl/task.h': TASK_H},
                           ['include', 'include/foo'])
    archive = deploy_and_archive(tmp_path, target, f'{build}/deploy/libfoo')
    names = [n for n in zipfile.ZipFile(archive).namelist() if not n.endswith('/')]
    assert sorted(names) == ['include/foo/foo.h', 'include/foo/impl/task.h', 'lib/libfoo.a', 'papa.txt']
    assert len(names) == len(set(names))


def test_a_nested_include_record_stays_in_papa_txt(tmp_path):
    # the archive drops the nested record, but the consumer still adds both include paths
    build, target = _built(tmp_path, {'include/foo/foo.h': '#pragma once\n'}, ['include', 'include/foo'])
    deploy_and_archive(tmp_path, target, f'{build}/deploy/libfoo')
    with zipfile.ZipFile(f'{tmp_path}/package.zip') as zip:
        lines = zip.read('papa.txt').decode().splitlines()
    assert [l for l in lines if l.startswith('I ')] == ['I include', 'I include/foo']


# --- two copies of one header tree are a packaging fault, whatever record ships them ---

def test_one_include_record_holding_two_copies_of_a_tree_fails_the_upload(tmp_path):
    # the real qcoro layout: cmake installs the headers into include/qcoro AND include/qcoro6/qcoro
    package = _deployed(tmp_path, {'include/qcoro/config.h': TASK_H, 'include/qcoro6/qcoro/config.h': TASK_H},
                        ['I include'])
    with pytest.raises(RuntimeError, match='duplicated directory pairs'):
        archive_papa_package(package, tmp_path / 'package.zip')


def test_the_duplicate_report_names_both_dirs_and_counts_the_files(tmp_path):
    package = _deployed(tmp_path, {'include/qcoro/config.h': TASK_H, 'include/qcoro/task.h': '// t\n',
                                   'include/qcoro6/qcoro/config.h': TASK_H,
                                   'include/qcoro6/qcoro/task.h': '// t\n'}, ['I include'])
    with pytest.raises(RuntimeError) as failure:
        archive_papa_package(package, tmp_path / 'package.zip')
    assert 'include/qcoro and include/qcoro6/qcoro hold 2 identical files: config.h, task.h' in str(failure.value)


def test_two_include_records_that_ship_one_tree_twice_fail_the_upload(tmp_path):
    package = _deployed(tmp_path, {'include/foo/task.h': TASK_H, 'foo/task.h': TASK_H}, ['I include', 'I foo'])
    with pytest.raises(RuntimeError, match='duplicated directory pairs'):
        archive_papa_package(package, tmp_path / 'package.zip')


def test_two_headers_of_one_size_with_different_content_upload_fine(tmp_path):
    # same name and same size, different content: the hash keeps this out of the duplicate report
    package = _deployed(tmp_path, {'include/a/cfg.h': '// aaa\n', 'include/b/cfg.h': '// bbb\n'},
                        ['I include/a', 'I include/b'])
    archive = archive_papa_package(package, tmp_path / 'package.zip')
    assert sorted(n for n in zipfile.ZipFile(archive).namelist() if not n.endswith('/')) == \
           ['include/a/cfg.h', 'include/b/cfg.h', 'papa.txt']


def test_a_second_export_include_inside_the_first_one_warns(tmp_path, capsys):
    _, target = _built(tmp_path, {'include/foo/foo.h': '#pragma once\n'}, [])
    target.export_include('include', build_dir=True)
    target.export_include('include/foo', build_dir=True)
    assert 'overlaps the exported' in capsys.readouterr().out


def test_the_deploy_warns_about_a_duplicated_tree_at_build_time(tmp_path, capsys):
    build, target = _built(tmp_path, {'include/qcoro/config.h': TASK_H,
                                      'include/qcoro6/qcoro/config.h': TASK_H}, ['include'])
    with pytest.raises(RuntimeError, match='duplicated directory pairs'):
        deploy_and_archive(tmp_path, target, f'{build}/deploy/libfoo')
    assert 'include/qcoro and include/qcoro6/qcoro hold 1 identical files' in capsys.readouterr().out
