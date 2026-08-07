"""Pins export_include(as_includes_root=...): the exported include path, the includes_root tuple,
the cmake defines, and the papa deploy layout that lets a consumer write #include <mylib/mylib.h>."""
import os
import pytest

from testutils import make_includes_dep, make_includes_target
import mama.package as package
from mama.dependency_chain import _get_dependency_cmake_defines
from mama.papa_deploy import _append_includes, papa_deploy_to, PapaFileInfo
from mama.utils.paths import normalized_path

@pytest.fixture
def nested(tmp_path):
    """tmp/src/mylib/ with mylib.h and internal.h, plus the target that exports from it."""
    d = tmp_path / 'src' / 'mylib'
    d.mkdir(parents=True)
    for name in ('mylib.h', 'internal.h'): (d / name).write_text('#pragma once\n')
    return make_includes_target(str(tmp_path))


@pytest.fixture
def flat(tmp_path):
    """The same two headers directly in tmp/src, with no subfolder to take the alias from."""
    d = tmp_path / 'src'
    d.mkdir(parents=True)
    for name in ('mylib.h', 'internal.h'): (d / name).write_text('#pragma once\n')
    return make_includes_target(str(tmp_path))


def _export(target, path, **kw):
    return package.export_include(target, path, build_dir=False, **kw)


def _under(target, *parts):
    return normalized_path(os.path.join(target.source_dir(), *parts))


# -- export_include state --

@pytest.mark.parametrize('alias', [True, 'mylib'], ids=['bool root', 'string alias'])
def test_a_root_export_records_the_parent_and_the_alias(nested, alias):
    # as_includes_root=True takes the basename, so it must land where the explicit spelling does
    assert _export(nested, 'src/mylib', as_includes_root=alias) is True
    assert nested.includes_root == (_under(nested, 'src'), _under(nested, 'src', 'mylib'), 'mylib')
    assert nested.exported_includes == [_under(nested, 'src')]  # the PARENT ships, so the alias is a path element


def test_a_string_alias_re_roots_a_flat_tree_under_that_name(flat):
    # as_includes_root='mylib' over src/ names the shipped dir, not the basename of the path
    assert _export(flat, 'src', as_includes_root='mylib') is True
    assert flat.includes_root == (_under(flat), _under(flat, 'src'), 'mylib')
    assert flat.exported_includes == [_under(flat)]


def test_a_plain_export_ships_the_dir_itself_and_sets_no_root(nested):
    _export(nested, 'src/mylib', as_includes_root=False)
    assert nested.exported_includes == [_under(nested, 'src', 'mylib')]
    assert nested.includes_root == ('', '', '')


def test_an_export_of_a_missing_path_changes_nothing(nested):
    assert _export(nested, 'nonexistent/path', as_includes_root=True) is False
    assert nested.exported_includes == [] and nested.includes_root == ('', '', '')


# -- cmake defines --

@pytest.mark.parametrize('as_root, shipped', [(True, 'src'), (False, 'src/mylib')])
def test_the_cmake_define_names_the_dir_the_consumer_adds(nested, as_root, shipped):
    _export(nested, 'src/mylib', as_includes_root=as_root)
    includes_var, cmake_text = _get_dependency_cmake_defines(make_includes_dep(nested))
    assert includes_var == '${TestLib_INCLUDES}'
    assert f'"{_under(nested, *shipped.split("/"))}"' in cmake_text
    if as_root:  # the re-rooted dir itself must not also reach the include path
        assert f'"{_under(nested, "src", "mylib")}"' not in cmake_text


# -- papa deploy --

def _deploy_includes(target, tmp_path):
    """Run the include half of a deploy and return (deploy dir, papa.txt records)."""
    deploy_dir = str(tmp_path / 'deploy' / 'TestLib')
    os.makedirs(deploy_dir, exist_ok=True)
    descr = []
    _append_includes(target, deploy_dir, False, descr, [(target, p) for p in target.exported_includes])
    return deploy_dir, descr


@pytest.mark.parametrize('fixture, path, alias', [('nested', 'src/mylib', True), ('flat', 'src', 'mylib')])
def test_a_root_export_deploys_the_headers_under_the_alias(request, tmp_path, fixture, path, alias):
    target = request.getfixturevalue(fixture)
    _export(target, path, as_includes_root=alias)
    deploy_dir, descr = _deploy_includes(target, tmp_path)
    for name in ('mylib.h', 'internal.h'):
        assert os.path.isfile(os.path.join(deploy_dir, 'include', 'mylib', name))
    assert not os.path.exists(os.path.join(deploy_dir, 'include', 'src'))  # the real dir name never ships
    assert descr == ['I include']  # the consumer adds `include`, then writes #include <mylib/mylib.h>


def test_a_plain_export_records_its_own_subpath(nested, tmp_path):
    _export(nested, 'src/mylib', as_includes_root=False)
    assert _deploy_includes(nested, tmp_path)[1] == ['I include/mylib']


def test_a_full_papa_deploy_writes_a_papa_file_a_reader_resolves(nested, tmp_path):
    _export(nested, 'src/mylib', as_includes_root=True)
    nested.config.test = False
    nested.children.return_value = []
    nested.is_current_target.return_value = False
    deploy_dir = str(tmp_path / 'deploy' / 'TestLib')
    os.makedirs(deploy_dir, exist_ok=True)
    papa_deploy_to(nested, deploy_dir, r_includes=False, r_dylibs=False, r_syslibs=False, r_assets=False)

    papa = PapaFileInfo(os.path.join(deploy_dir, 'papa.txt'))
    assert papa.project_name == 'TestLib'
    assert [normalized_path(p) for p in papa.includes] == [normalized_path(os.path.join(deploy_dir, 'include'))]
    assert os.path.isfile(os.path.join(deploy_dir, 'include', 'mylib', 'mylib.h'))
