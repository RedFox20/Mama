"""Pins every rule of papa_deploy._append_includes: which papa record each export writes, where the
headers land, and which export is skipped."""
import os

import pytest
from mama.papa_deploy import _append_includes
from testutils import make_exporting_target, make_mock_dep, write_files


@pytest.fixture
def producer(tmp_path):
    """A build dir to export from, a package dir to deploy into, and the target that owns both."""
    dep = make_mock_dep(tmp_path / 'producer', name='libfoo')
    target = make_exporting_target(dep, [], [])
    return dep.build_dir, str(tmp_path / 'package'), target


def _deploy(producer, files:dict, includes:list, detail_echo=False) -> tuple:
    """Deploy `includes` (build-dir relative) and return (papa records, deployed file paths)."""
    build, package, target = producer
    write_files(build, files)
    descr = []
    _append_includes(target, package, detail_echo, descr, [(target, f'{build}/{i}') for i in includes])
    deployed = sorted(os.path.relpath(os.path.join(d, f), package).replace('\\', '/')
                      for d, _, names in os.walk(package) for f in names)
    return descr, deployed


def test_no_exported_include_writes_no_record(producer):
    _, package, target = producer
    descr = []
    _append_includes(target, package, False, descr, [])
    assert descr == [] and not os.path.exists(package)


def test_a_dir_named_include_deploys_into_the_package_include_root(producer):
    descr, deployed = _deploy(producer, {'include/foo.h': '// foo\n'}, ['include'])
    assert descr == ['I include'] and deployed == ['include/foo.h']


def test_any_other_dir_name_deploys_under_its_own_name(producer):
    descr, deployed = _deploy(producer, {'src/foo.h': '// foo\n'}, ['src'])
    assert descr == ['I include/src'] and deployed == ['include/src/foo.h']


def test_as_includes_root_renames_the_dir_to_its_alias(producer):
    build, _, target = producer
    write_files(build, {'src/mylib/foo.h': '// foo\n'})
    target.export_include('src/mylib', build_dir=True, as_includes_root='mylib')
    descr, deployed = _deploy(producer, {}, ['src'])
    assert descr == ['I include'] and deployed == ['include/mylib/foo.h']


def test_a_nested_dir_keeps_its_subdirs(producer):
    descr, deployed = _deploy(producer, {'lib/a.h': '// a\n', 'lib/impl/b.h': '// b\n'}, ['lib'])
    assert descr == ['I include/lib'] and deployed == ['include/lib/a.h', 'include/lib/impl/b.h']


def test_every_export_writes_its_record_in_export_order(producer):
    descr, deployed = _deploy(producer, {'a/a.h': '// a\n', 'b/b.h': '// b\n'}, ['a', 'b'])
    assert descr == ['I include/a', 'I include/b'] and deployed == ['include/a/a.h', 'include/b/b.h']


def test_a_second_export_of_one_dir_name_is_skipped(producer):
    # the dedup key is the dir name, so the second `foo` never reaches the package
    descr, deployed = _deploy(producer, {'x/foo/a.h': '// a\n', 'y/foo/b.h': '// b\n'}, ['x/foo', 'y/foo'])
    assert descr == ['I include/foo'] and deployed == ['include/foo/a.h']


def test_only_the_glob_filter_suffixes_are_copied(producer):
    files = {'include/foo.h': '// h\n', 'include/foo.hpp': '// hpp\n', 'include/foo.txt': 'text\n',
             'include/foo.cpp': '// cpp\n'}
    descr, deployed = _deploy(producer, files, ['include'])
    assert deployed == ['include/foo.h', 'include/foo.hpp']


def test_an_include_already_inside_the_package_is_not_copied_onto_itself(producer):
    build, _, target = producer
    write_files(build, {'include/foo.h': '// foo\n'})
    descr = []
    _append_includes(target, build, False, descr, [(target, f'{build}/include')])  # package IS the build dir
    assert descr == ['I include'] and os.listdir(f'{build}/include') == ['foo.h']


def test_detail_echo_names_every_deployed_include(producer, capsys):
    _deploy(producer, {'a/a.h': '// a\n', 'include/b.h': '// b\n'}, ['a', 'include'], detail_echo=True)
    out = capsys.readouterr().out
    assert 'include/a' in out and out.count('libfoo') == 2


def test_verbose_names_the_copy(producer, capsys):
    _, _, target = producer
    target.config.verbose = True
    _deploy(producer, {'a/a.h': '// a\n'}, ['a'])
    assert 'copy' in capsys.readouterr().out
