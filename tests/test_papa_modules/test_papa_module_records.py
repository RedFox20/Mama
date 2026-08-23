"""Pins the `M` records a deploy writes, and that the module ships once inside the include tree."""
import os
import zipfile
from unittest.mock import patch

import pytest
from testutils import (deploy_and_archive, make_exporting_target, make_mock_dep, papa_deploy_target,
                       write_files)

from mama import package
from mama.papa_deploy import PapaFileInfo
from mama.papa_upload import validate_archive

CPPM = 'module;\n#include "rpp/strview.h"\nexport module rpp.strview;\n'
MODULE = 'include/rpp/rpp-strview.cppm'
# both layouts a recipe uses: a plain include/ tree, and a src/ tree re-rooted by as_includes_root
FILES = {'include/rpp/strview.h': '#pragma once\n', 'include/rpp/rpp-strview.cppm': CPPM,
         'include/rpp/rpp-strview.ixx': CPPM,  # a second extension, so a filter cannot answer for both
         'src/rpp/strview.h': '#pragma once\n', 'src/rpp/rpp-strview.cppm': CPPM, 'lib/libfoo.a': '\0'}


def _built(tmp_path, includes, modules, filter=None, as_root=False, files=FILES):
    """A producer build dir, and the target that exports `includes` plus `modules`."""
    dep = make_mock_dep(tmp_path / 'producer', name='libfoo')
    build = dep.build_dir
    write_files(build, files)
    target = make_exporting_target(dep, [f'{build}/{i}' for i in includes], [f'{build}/lib/libfoo.a'],
                                   modules=[f'{build}/{m}' for m in modules])
    if filter is not None: target.include_glob_filter = filter
    if as_root: target.includes_root = (f'{build}/src', f'{build}/src/rpp', 'rpp')
    target.strip_module_objects = False  # these pin the records, and the fixture lib is not an archive
    return build, target


def _deploy(tmp_path, includes=('include',), modules=(MODULE,), **kw):
    """Build, deploy and archive one producer. Returns (build dir, target, archive path)."""
    build, target = _built(tmp_path, list(includes), list(modules), **kw)
    return build, target, deploy_and_archive(tmp_path, target, f'{build}/deploy/libfoo')


def _papa_lines(archive) -> list:
    with zipfile.ZipFile(archive) as zip:
        return zip.read('papa.txt').decode().splitlines()


def _names(archive) -> list:
    return zipfile.ZipFile(archive).namelist()


# --- the records a deploy writes ---------------------------------------------

def test_a_module_gets_an_m_record_under_the_include_tree(tmp_path):
    assert f'M {MODULE}' in _papa_lines(_deploy(tmp_path)[2])


def test_an_includes_root_export_records_the_aliased_module_path(tmp_path):
    # as_includes_root deploys src/rpp as include/rpp, and the module record follows that alias
    _, _, archive = _deploy(tmp_path, ['src'], ['src/rpp/rpp-strview.cppm'], as_root=True)
    assert f'M {MODULE}' in _papa_lines(archive)


def test_the_module_ships_although_the_header_filter_names_no_cppm(tmp_path):
    # export_modules carries its own suffixes, so the hook order cannot drop a module
    assert MODULE in _names(_deploy(tmp_path, filter=['.h'])[2])


def test_the_module_appears_in_the_archive_exactly_once(tmp_path):
    assert _names(_deploy(tmp_path)[2]).count(MODULE) == 1


def test_an_uppercase_module_extension_still_ships(tmp_path):
    # the glob reads Api.IXX as a module, and a case-sensitive suffix test then dropped it silently
    files = {'include/rpp/Api.IXX': CPPM, 'lib/libfoo.a': '\0'}
    _, _, archive = _deploy(tmp_path, modules=['include/rpp/Api.IXX'], files=files)
    assert 'M include/rpp/Api.IXX' in _papa_lines(archive)
    assert 'include/rpp/Api.IXX' in _names(archive)


def test_a_cppm_carried_by_the_include_filter_alone_writes_no_m_record(tmp_path):
    # requirement 1: the filter already moves any suffix, and that alone declares no module
    _, _, archive = _deploy(tmp_path, modules=(), filter=['.h', '.cppm'])
    assert MODULE in _names(archive)
    assert not [l for l in _papa_lines(archive) if l.startswith('M ')]


# --- an export decides which module files ship -------------------------------

@pytest.mark.parametrize('exported, private', [
    ('include/rpp/rpp-strview.cppm', 'include/rpp/private.cppm'),
    # the filter names .cppm and the export names a .ixx, so only the exported list may decide
    ('include/rpp/rpp-strview.ixx', 'include/rpp/rpp-strview.cppm'),
])
def test_a_target_that_exports_a_module_answers_for_every_module_file(tmp_path, exported, private):
    build, target = _built(tmp_path, ['include'], [exported], filter=['.h', '.cppm'])
    write_files(build, {'include/rpp/private.cppm': CPPM})
    names = _names(deploy_and_archive(tmp_path, target, f'{build}/deploy/libfoo'))
    assert exported in names and private not in names


def test_a_private_module_sharing_a_tail_path_with_an_exported_one_stays_out(tmp_path):
    # `private/sub/api.cppm` ends with the same tail as the exported `sub/api.cppm`
    files = {'include/sub/api.cppm': CPPM, 'include/private/sub/api.cppm': CPPM,
             'include/sub/api.h': '#pragma once\n', 'lib/libfoo.a': '\0'}
    _, _, archive = _deploy(tmp_path, modules=['include/sub/api.cppm'], files=files)
    assert 'include/sub/api.cppm' in _names(archive)
    assert 'include/private/sub/api.cppm' not in _names(archive)


def test_a_module_outside_every_exported_include_warns_and_records_nothing(tmp_path):
    build, target = _built(tmp_path, ['include'], ['src/rpp/rpp-strview.cppm'])
    with patch('mama.package.warning') as warned:
        package.warn_unreachable_modules(target)
    assert 'rpp-strview.cppm' in warned.call_args[0][0]  # every later step drops it without a word
    archive = deploy_and_archive(tmp_path, target, f'{build}/deploy/libfoo')
    assert not [l for l in _papa_lines(archive) if l.startswith('M ')]


def test_an_includes_root_export_holds_no_module_outside_its_subtree(tmp_path):
    # as_includes_root deploys src/rpp alone, so a module under src/other never reaches a consumer
    build, target = _built(tmp_path, ['src'], ['src/rpp/rpp-strview.cppm'], as_root=True)
    write_files(build, {'src/other/hidden.cppm': CPPM})
    assert package.module_base_dir(target, f'{build}/src/other/hidden.cppm') == ''


@pytest.mark.case_sensitive_fs
def test_a_case_variant_dir_holds_no_module_of_the_other_one(tmp_path):
    # a case-sensitive volume keeps Src/ and src/ apart, and folding the case merges two real dirs
    write_files(str(tmp_path), {'Src/api.cppm': CPPM, 'src/other.h': '#pragma once\n'})
    target = make_exporting_target(make_mock_dep(tmp_path, name='libfoo'), [f'{tmp_path}/src'], [])
    with patch('mama.package.System') as system:
        system.macos = True  # the platform that folds case, on a volume that does not
        assert package.module_base_dir(target, f'{tmp_path}/Src/api.cppm') == ''


# --- the upload validation ---------------------------------------------------

def test_an_m_record_the_archive_does_not_hold_fails_the_upload(tmp_path):
    build, _, archive = _deploy(tmp_path)
    deployed = f'{build}/deploy/libfoo'
    papa = PapaFileInfo(f'{deployed}/papa.txt')
    papa.modules.append(f'{deployed}/include/rpp/rpp-gone.cppm')
    with pytest.raises(RuntimeError, match='include filter dropped'):
        validate_archive(deployed, papa, archive)


def test_a_casing_variant_of_a_shipped_module_passes_the_upload(tmp_path):
    # the walk answers the casing on disk, and the M record keeps the casing of the recipe
    build, _, archive = _deploy(tmp_path)
    deployed = f'{build}/deploy/libfoo'
    papa = PapaFileInfo(f'{deployed}/papa.txt')
    papa.modules = [m.replace('rpp-strview', 'RPP-Strview') for m in papa.modules]
    with patch.object(package.System, 'macos', True):  # a case-insensitive filesystem holds one file
        validate_archive(deployed, papa, archive)


# --- an in-place deploy, and a recursive bundle -------------------------------

@pytest.mark.parametrize('symlinked', [False, True])
def test_an_in_place_deploy_of_a_module_package_is_refused(tmp_path, symlinked):
    # a string compare calls a symlink and its target different, and the strip would then edit the
    # build artifact. The refusal fires before any archive is read.
    build, target = _built(tmp_path, ['include'], [MODULE])
    target.strip_module_objects = True
    where = str(tmp_path / 'linked')
    if symlinked: os.symlink(build, where)
    with pytest.raises(RuntimeError, match='is the build output itself'):
        papa_deploy_target(target, where if symlinked else build)


def test_an_in_place_deploy_without_the_strip_still_works(tmp_path):
    build, target = _built(tmp_path, ['include'], [MODULE])
    papa_deploy_target(target, build)
    assert os.path.exists(f'{build}/papa.txt')


def test_a_recursive_deploy_writes_no_m_record_for_a_child_module(tmp_path):
    # the child package ships its own M record, and two copies make cmake refuse the second one
    build, parent = _built(tmp_path, ['include'], [], filter=['.h'])
    child = make_mock_dep(tmp_path / 'child', name='libchild')
    write_files(child.build_dir, FILES)
    child.target = make_exporting_target(child, [f'{child.build_dir}/include'], [],
                                         modules=[f'{child.build_dir}/{MODULE}'])
    deployed = f'{build}/deploy/libfoo'
    papa_deploy_target(parent, deployed, r_includes=True, children=[child])
    lines = open(f'{deployed}/papa.txt').read().splitlines()
    assert not [l for l in lines if l.startswith('M ')]
    assert [l for l in lines if l.startswith('D ')], 'the child still arrives as a dependency'


def _recursive_deploy(tmp_path, parent_modules, child_modules):
    """Deploy a parent whose include tree physically holds the child's. Returns the papa dir."""
    build, parent = _built(tmp_path, ['include'], parent_modules, filter=['.h', '.cppm'])
    write_files(build, {'include/child/extra.cppm': CPPM, 'include/rpp/loose.cppm': CPPM})
    child = make_mock_dep(tmp_path / 'child', name='libchild')
    child.build_dir = build  # the bundled child tree sits inside the parent's include root
    child.target = make_exporting_target(child, [f'{build}/include/child'], [],
                                         modules=[f'{build}/{m}' for m in child_modules])
    deployed = str(tmp_path / 'deploy' / 'libfoo')
    papa_deploy_target(parent, deployed, r_includes=True, children=[child])
    return deployed


def test_a_bundled_child_tree_that_exports_no_module_falls_back_to_the_filter(tmp_path):
    # the child exports no module, so the deploy include filter ships its .cppm. One flat root set
    # gated every descendant of the parent's module base and dropped it silently.
    deployed = _recursive_deploy(tmp_path, [MODULE], [])
    assert os.path.exists(f'{deployed}/include/child/extra.cppm')
    # the parent DID export a module, so its own tree still answers from that list alone
    assert not os.path.exists(f'{deployed}/include/rpp/loose.cppm')


def test_a_bundled_child_module_gates_its_own_tree_and_no_other(tmp_path):
    # the child owns the module, so only the child's base dir gates. A base resolved against the
    # parent answered '', which gated every absolute path and dropped the parent's own sources.
    deployed = _recursive_deploy(tmp_path, [], ['include/child/extra.cppm'])
    assert os.path.exists(f'{deployed}/include/rpp/loose.cppm')


@pytest.mark.case_sensitive_fs
def test_a_case_variant_deploy_dir_is_not_the_build_dir(tmp_path):
    # a case-sensitive volume holds build/ and BUILD/ apart, and folding read the deploy dir as the
    # build dir. That refused the deploy, and on the lib loop it skipped the copy and recorded nothing.
    build, target = _built(tmp_path, ['include'], [MODULE])
    target.strip_module_objects = True
    variant = f'{os.path.dirname(build)}/{os.path.basename(build).upper()}'
    with patch.object(package.System, 'macos', True), \
         patch.object(package, 'strip_module_objects'):  # the fixture lib is not a real archive
        papa_deploy_target(target, variant)
    assert os.path.exists(f'{variant}/papa.txt') and os.path.exists(f'{variant}/lib/libfoo.a')
