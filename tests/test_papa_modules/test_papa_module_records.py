"""Pins the `M` records a deploy writes, and that the module ships once inside the include tree."""
import os
import zipfile
from unittest.mock import patch

import pytest
from testutils import deploy_and_archive, make_exporting_target, make_mock_dep, write_files

CPPM = 'module;\n#include "rpp/strview.h"\nexport module rpp.strview;\n'
# both layouts a recipe uses: a plain include/ tree, and a src/ tree re-rooted by as_includes_root
FILES = {'include/rpp/strview.h': '#pragma once\n', 'include/rpp/rpp-strview.cppm': CPPM,
         'src/rpp/strview.h': '#pragma once\n', 'src/rpp/rpp-strview.cppm': CPPM, 'lib/libfoo.a': '\0'}


def _built(tmp_path, includes, modules, filter=None, as_root=False):
    """A producer build dir, and the target that exports `includes` plus `modules`."""
    dep = make_mock_dep(tmp_path / 'producer', name='libfoo')
    build = dep.build_dir
    write_files(build, FILES)
    target = make_exporting_target(dep, [f'{build}/{i}' for i in includes], [f'{build}/lib/libfoo.a'],
                                   modules=[f'{build}/{m}' for m in modules])
    if filter is not None: target.include_glob_filter = filter
    if as_root: target.includes_root = (f'{build}/src', f'{build}/src/rpp', 'rpp')
    return build, target


def _papa_lines(archive) -> list:
    with zipfile.ZipFile(archive) as zip:
        return zip.read('papa.txt').decode().splitlines()


def test_a_module_gets_an_m_record_under_the_include_tree(tmp_path):
    build, target = _built(tmp_path, ['include'], ['include/rpp/rpp-strview.cppm'])
    archive = deploy_and_archive(tmp_path, target, f'{build}/deploy/libfoo')
    assert 'M include/rpp/rpp-strview.cppm' in _papa_lines(archive)


def test_an_includes_root_export_records_the_aliased_module_path(tmp_path):
    # as_includes_root deploys src/rpp as include/rpp, and the module record follows that alias
    build, target = _built(tmp_path, ['src'], ['src/rpp/rpp-strview.cppm'], as_root=True)
    archive = deploy_and_archive(tmp_path, target, f'{build}/deploy/libfoo')
    assert 'M include/rpp/rpp-strview.cppm' in _papa_lines(archive)


def test_the_module_ships_although_the_header_filter_names_no_cppm(tmp_path):
    # export_modules carries its own suffixes, so the hook order cannot drop a module
    build, target = _built(tmp_path, ['include'], ['include/rpp/rpp-strview.cppm'], filter=['.h'])
    archive = deploy_and_archive(tmp_path, target, f'{build}/deploy/libfoo')
    assert 'include/rpp/rpp-strview.cppm' in zipfile.ZipFile(archive).namelist()


def test_the_module_appears_in_the_archive_exactly_once(tmp_path):
    build, target = _built(tmp_path, ['include'], ['include/rpp/rpp-strview.cppm'])
    archive = deploy_and_archive(tmp_path, target, f'{build}/deploy/libfoo')
    assert zipfile.ZipFile(archive).namelist().count('include/rpp/rpp-strview.cppm') == 1


def test_a_cppm_carried_by_the_include_filter_alone_writes_no_m_record(tmp_path):
    # requirement 1: the filter already moves any suffix, and that alone declares no module
    build, target = _built(tmp_path, ['include'], [], filter=['.h', '.cppm'])
    archive = deploy_and_archive(tmp_path, target, f'{build}/deploy/libfoo')
    assert 'include/rpp/rpp-strview.cppm' in zipfile.ZipFile(archive).namelist()
    assert not [l for l in _papa_lines(archive) if l.startswith('M ')]


def test_a_module_the_export_never_named_stays_out_of_the_package(tmp_path):
    build, target = _built(tmp_path, ['include'], ['include/rpp/rpp-strview.cppm'])
    write_files(build, {'include/rpp/private.cppm': CPPM})
    names = zipfile.ZipFile(deploy_and_archive(tmp_path, target, f'{build}/deploy/libfoo')).namelist()
    assert 'include/rpp/rpp-strview.cppm' in names and 'include/rpp/private.cppm' not in names


def test_a_module_outside_every_exported_include_warns_and_records_nothing(tmp_path):
    build, target = _built(tmp_path, ['include'], ['src/rpp/rpp-strview.cppm'])
    archive = deploy_and_archive(tmp_path, target, f'{build}/deploy/libfoo')
    assert not [l for l in _papa_lines(archive) if l.startswith('M ')]


def test_a_recursive_deploy_carries_a_child_module_the_parent_filter_never_names(tmp_path):
    # the deploy filter reads the gathered modules, so a child suffix reaches the copy too
    from mama import papa_deploy
    build, parent = _built(tmp_path, ['include'], [], filter=['.h'])
    child_dep = make_mock_dep(tmp_path / 'child', name='libchild')
    write_files(child_dep.build_dir, FILES)
    child_dep.target = make_exporting_target(child_dep, [f'{child_dep.build_dir}/include'], [],
                                             modules=[f'{child_dep.build_dir}/include/rpp/rpp-strview.cppm'])
    package = f'{build}/deploy/libfoo'
    # the patch replaces the method on the class, so the child must answer with no children of its own
    with patch.object(type(parent), 'children', lambda self: [child_dep] if self is parent else []):
        papa_deploy.papa_deploy_to(parent, package, r_includes=True, r_dylibs=False,
                                   r_syslibs=False, r_assets=False)
    lines = open(f'{package}/papa.txt').read().splitlines()
    assert [l for l in lines if l.startswith('M ')] == ['M include/rpp/rpp-strview.cppm']
    assert os.path.exists(f'{package}/include/rpp/rpp-strview.cppm')


def test_an_m_record_the_archive_does_not_hold_fails_the_upload(tmp_path):
    from mama.papa_deploy import PapaFileInfo
    from mama.papa_upload import validate_archive
    build, target = _built(tmp_path, ['include'], ['include/rpp/rpp-strview.cppm'])
    archive = deploy_and_archive(tmp_path, target, f'{build}/deploy/libfoo')
    package = f'{build}/deploy/libfoo'
    papa = PapaFileInfo(f'{package}/papa.txt')
    papa.modules.append(f'{package}/include/rpp/rpp-gone.cppm')
    with pytest.raises(RuntimeError, match='include filter dropped'):
        validate_archive(package, papa, archive)
