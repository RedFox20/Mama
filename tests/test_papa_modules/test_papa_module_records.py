"""Pins the `M` records a deploy writes, and that the module ships once inside the include tree."""
import os
import zipfile
from unittest.mock import patch

import pytest
from testutils import deploy_and_archive, make_exporting_target, make_mock_dep, write_files

from mama import package

CPPM = 'module;\n#include "rpp/strview.h"\nexport module rpp.strview;\n'
# both layouts a recipe uses: a plain include/ tree, and a src/ tree re-rooted by as_includes_root
FILES = {'include/rpp/strview.h': '#pragma once\n', 'include/rpp/rpp-strview.cppm': CPPM,
         'include/rpp/rpp-strview.ixx': CPPM,  # a second extension, so a filter cannot answer for both
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
    target.strip_module_objects = False  # these pin the records, and the fixture lib is not a real archive
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


def test_a_filter_that_names_a_module_suffix_still_ships_the_exported_module_alone(tmp_path):
    # a target that exports modules answers from that list, so no filter can widen it to a private one
    build, target = _built(tmp_path, ['include'], ['include/rpp/rpp-strview.cppm'], filter=['.h', '.cppm'])
    write_files(build, {'include/rpp/private.cppm': CPPM})
    names = zipfile.ZipFile(deploy_and_archive(tmp_path, target, f'{build}/deploy/libfoo')).namelist()
    assert 'include/rpp/rpp-strview.cppm' in names and 'include/rpp/private.cppm' not in names


def test_a_module_the_export_never_named_stays_out_of_the_package(tmp_path):
    build, target = _built(tmp_path, ['include'], ['include/rpp/rpp-strview.cppm'])
    write_files(build, {'include/rpp/private.cppm': CPPM})
    names = zipfile.ZipFile(deploy_and_archive(tmp_path, target, f'{build}/deploy/libfoo')).namelist()
    assert 'include/rpp/rpp-strview.cppm' in names and 'include/rpp/private.cppm' not in names


def test_a_module_outside_every_exported_include_warns_and_records_nothing(tmp_path):
    build, target = _built(tmp_path, ['include'], ['src/rpp/rpp-strview.cppm'])
    with patch('mama.package.warning') as warned:
        package.warn_unreachable_modules(target)
    assert 'rpp-strview.cppm' in warned.call_args[0][0]  # every later step drops it without a word
    archive = deploy_and_archive(tmp_path, target, f'{build}/deploy/libfoo')
    assert not [l for l in _papa_lines(archive) if l.startswith('M ')]


def test_a_private_module_of_another_extension_stays_out(tmp_path):
    # the filter names .cppm and the export names a .ixx, so only the exported list may decide
    build, target = _built(tmp_path, ['include'], ['include/rpp/rpp-strview.ixx'], filter=['.h', '.cppm'])
    names = zipfile.ZipFile(deploy_and_archive(tmp_path, target, f'{build}/deploy/libfoo')).namelist()
    assert 'include/rpp/rpp-strview.ixx' in names and 'include/rpp/rpp-strview.cppm' not in names


def test_an_includes_root_export_holds_no_module_outside_its_subtree(tmp_path):
    # as_includes_root deploys src/rpp alone, so a module under src/other never reaches a consumer
    build, target = _built(tmp_path, ['src'], ['src/rpp/rpp-strview.cppm'], as_root=True)
    write_files(build, {'src/other/hidden.cppm': CPPM})
    target.exported_modules.append(f'{build}/src/other/hidden.cppm')
    assert package.module_base_dir(target, f'{build}/src/other/hidden.cppm') == ''


def test_a_recursive_deploy_writes_no_m_record_for_a_child_module(tmp_path):
    # the parent writes a D record for the child, and the child package ships its own M record. Two
    # copies would make a consumer compile one module twice, and cmake refuses the second declaration.
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
    assert not [l for l in lines if l.startswith('M ')]
    assert [l for l in lines if l.startswith('D ')], 'the child still arrives as a dependency'


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


def test_a_casing_variant_of_a_shipped_module_passes_the_upload(tmp_path):
    # the walk answers the casing on disk, and the M record keeps the casing of the recipe
    from mama.papa_deploy import PapaFileInfo
    from mama.papa_upload import validate_archive
    from mama import package as pkg
    build, target = _built(tmp_path, ['include'], ['include/rpp/rpp-strview.cppm'])
    archive = deploy_and_archive(tmp_path, target, f'{build}/deploy/libfoo')
    deployed = f'{build}/deploy/libfoo'
    papa = PapaFileInfo(f'{deployed}/papa.txt')
    papa.modules = [m.replace('rpp-strview', 'RPP-Strview') for m in papa.modules]
    with patch.object(pkg.System, 'macos', True):  # a case-insensitive filesystem holds one file
        validate_archive(deployed, papa, archive)


def test_a_private_module_sharing_a_tail_path_with_an_exported_one_stays_out(tmp_path):
    # `private/sub/api.cppm` ends with the same tail as the exported `sub/api.cppm`
    dep = make_mock_dep(tmp_path / 'producer', name='libfoo')
    build = dep.build_dir
    write_files(build, {'include/sub/api.cppm': CPPM, 'include/private/sub/api.cppm': CPPM,
                        'include/sub/api.h': '#pragma once\n', 'lib/libfoo.a': '\0'})
    target = make_exporting_target(dep, [f'{build}/include'], [f'{build}/lib/libfoo.a'],
                                   modules=[f'{build}/include/sub/api.cppm'])
    target.strip_module_objects = False  # the fixture lib is not a real archive
    archive = deploy_and_archive(tmp_path, target, f'{build}/deploy/libfoo')
    names = zipfile.ZipFile(archive).namelist()
    assert 'include/sub/api.cppm' in names
    assert 'include/private/sub/api.cppm' not in names


def test_an_in_place_deploy_of_a_module_package_is_refused(tmp_path):
    # the package and the build artifact are one file, so the strip would take what the producer links
    from mama import papa_deploy
    build, target = _built(tmp_path, ['include'], ['include/rpp/rpp-strview.cppm'])
    target.strip_module_objects = True  # the refusal fires before any archive is read
    with patch.object(type(target), 'children', lambda self: []):
        with pytest.raises(RuntimeError, match='is the build dir itself'):
            papa_deploy.papa_deploy_to(target, build, r_includes=False, r_dylibs=False,
                                       r_syslibs=False, r_assets=False)


def test_an_in_place_deploy_without_the_strip_still_works(tmp_path):
    from mama import papa_deploy
    build, target = _built(tmp_path, ['include'], ['include/rpp/rpp-strview.cppm'])
    target.strip_module_objects = False
    with patch.object(type(target), 'children', lambda self: []):
        papa_deploy.papa_deploy_to(target, build, r_includes=False, r_dylibs=False,
                                   r_syslibs=False, r_assets=False)
    assert os.path.exists(f'{build}/papa.txt')


def test_a_symlinked_deploy_dir_is_still_the_build_artifact(tmp_path):
    # a string compare calls the two paths different, and the strip would edit the build artifact
    from mama import papa_deploy
    build, target = _built(tmp_path, ['include'], ['include/rpp/rpp-strview.cppm'])
    target.strip_module_objects = True
    link = str(tmp_path / 'linked')
    os.symlink(build, link)
    with patch.object(type(target), 'children', lambda self: []):
        with pytest.raises(RuntimeError, match='is the build dir itself'):
            papa_deploy.papa_deploy_to(target, link, r_includes=False, r_dylibs=False,
                                       r_syslibs=False, r_assets=False)


@pytest.mark.case_sensitive_fs
def test_a_case_variant_dir_holds_no_module_of_the_other_one(tmp_path):
    # a case-sensitive volume keeps Src/ and src/ apart, and folding the case merges two real dirs
    write_files(str(tmp_path), {'Src/api.cppm': CPPM, 'src/other.h': '#pragma once\n'})
    target = make_exporting_target(make_mock_dep(tmp_path, name='libfoo'), [f'{tmp_path}/src'], [])
    with patch('mama.package.System') as system:
        system.macos = True  # the platform that folds case, on a volume that does not
        assert package.module_base_dir(target, f'{tmp_path}/Src/api.cppm') == ''


def test_an_uppercase_module_extension_still_ships(tmp_path):
    # the glob reads Api.IXX as a module, and a case-sensitive suffix test then dropped it silently
    dep = make_mock_dep(tmp_path / 'producer', name='libfoo')
    build = dep.build_dir
    write_files(build, {'include/rpp/Api.IXX': CPPM, 'lib/libfoo.a': '\0'})
    target = make_exporting_target(dep, [f'{build}/include'], [f'{build}/lib/libfoo.a'],
                                   modules=[f'{build}/include/rpp/Api.IXX'])
    target.strip_module_objects = False
    archive = deploy_and_archive(tmp_path, target, f'{build}/deploy/libfoo')
    assert 'M include/rpp/Api.IXX' in _papa_lines(archive)
    assert 'include/rpp/Api.IXX' in zipfile.ZipFile(archive).namelist()


def _recursive_deploy(tmp_path, parent_modules, child_modules, filter):
    """Deploy a parent whose include tree physically holds the child's. Returns the papa dir."""
    from mama import papa_deploy
    build, parent = _built(tmp_path, ['include'], parent_modules, filter=filter)
    write_files(build, {'include/child/extra.cppm': CPPM, 'include/rpp/loose.cppm': CPPM})
    child_dep = make_mock_dep(tmp_path / 'child', name='libchild')
    child_dep.build_dir = build  # the bundled child tree sits inside the parent's include root
    child_dep.target = make_exporting_target(child_dep, [f'{build}/include/child'], [],
                                             modules=[f'{build}/{m}' for m in child_modules])
    deployed = str(tmp_path / 'deploy' / 'libfoo')
    with patch.object(type(parent), 'children', lambda self: [child_dep] if self is parent else []):
        papa_deploy.papa_deploy_to(parent, deployed, r_includes=True, r_dylibs=False,
                                   r_syslibs=False, r_assets=False)
    return deployed


def test_a_bundled_child_tree_that_exports_no_module_falls_back_to_the_filter(tmp_path):
    # the child exports no module, so its .cppm rides the include filter of the deploy. One flat root
    # set gated every descendant of the parent's module base, and dropped the file with no error.
    deployed = _recursive_deploy(tmp_path, ['include/rpp/rpp-strview.cppm'], [], ['.h', '.cppm'])
    assert os.path.exists(f'{deployed}/include/child/extra.cppm')
    # the parent DID export a module, so its own tree still answers from that list alone
    assert not os.path.exists(f'{deployed}/include/rpp/loose.cppm')


def test_a_bundled_child_module_gates_its_own_tree_and_no_other(tmp_path):
    # the child owns the module, so only the child's base dir gates. A base resolved against the
    # parent answered '', which gated every absolute path and dropped the parent's own sources.
    deployed = _recursive_deploy(tmp_path, [], ['include/child/extra.cppm'], ['.h', '.cppm'])
    assert os.path.exists(f'{deployed}/include/rpp/loose.cppm')
