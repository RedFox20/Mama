"""Pins export_include(as_includes_root=...): the exported include path, the includes_root tuple,
the cmake defines, and the papa deploy layout that lets a consumer write #include <mylib/mylib.h>."""
import os
import tempfile
import shutil
from unittest.mock import Mock

from mama.utils.paths import normalized_path
import mama.package as package
from mama.papa_deploy import _append_includes, papa_deploy_to, PapaFileInfo
from mama.dependency_chain import _get_dependency_cmake_defines
from mama.platforms.linux import Linux


def make_mock_target(source_dir, build_dir=None):
    target = Mock()
    target.source_dir.return_value = normalized_path(source_dir)
    target.build_dir.return_value = normalized_path(build_dir or os.path.join(source_dir, 'build'))
    target.exported_includes = []
    target.exported_libs = []
    target.exported_syslibs = []
    target.exported_assets = []
    target.includes_root = ('', '', '')
    target.include_glob_filter = ['.h', '.hpp', '.hxx', '.hh']
    target.name = 'TestLib'
    target.config.platform = Linux(target.config)
    return target


def make_mock_dep(target, name='TestLib', children=None):
    dep = Mock()
    dep.name = name
    dep.target = target
    dep.children = children or []
    dep.get_children.return_value = dep.children
    return dep


def make_temp_lib_tree():
    """tmpdir/src/mylib/ with mylib.h and internal.h."""
    tmpdir = tempfile.mkdtemp(prefix='mama_test_')
    mylib = os.path.join(tmpdir, 'src', 'mylib')
    os.makedirs(mylib)
    with open(os.path.join(mylib, 'mylib.h'), 'w') as f:
        f.write('#pragma once\nint mylib_func();\n')
    with open(os.path.join(mylib, 'internal.h'), 'w') as f:
        f.write('#pragma once\nint internal_func();\n')
    return tmpdir


def make_temp_flat_lib_tree():
    """tmpdir/src/ with mylib.h and internal.h directly inside, no subfolder."""
    tmpdir = tempfile.mkdtemp(prefix='mama_test_')
    src = os.path.join(tmpdir, 'src')
    os.makedirs(src)
    with open(os.path.join(src, 'mylib.h'), 'w') as f:
        f.write('#pragma once\nint mylib_func();\n')
    with open(os.path.join(src, 'internal.h'), 'w') as f:
        f.write('#pragma once\nint internal_func();\n')
    return tmpdir


# -- export_include state --

def test_export_include_sets_includes_root():
    # includes_root is (parent_path, src_path, alias_name)
    tmpdir = make_temp_lib_tree()
    try:
        target = make_mock_target(tmpdir)
        result = package.export_include(target, 'src/mylib', build_dir=False,
                                        as_includes_root=True)
        assert result is True

        src_dir = normalized_path(os.path.join(tmpdir, 'src'))
        mylib_dir = normalized_path(os.path.join(tmpdir, 'src', 'mylib'))
        assert target.includes_root == (src_dir, mylib_dir, 'mylib')
    finally:
        shutil.rmtree(tmpdir)


def test_export_include_adds_parent_to_exported_includes():
    tmpdir = make_temp_lib_tree()
    try:
        target = make_mock_target(tmpdir)
        package.export_include(target, 'src/mylib', build_dir=False,
                               as_includes_root=True)

        src_dir = normalized_path(os.path.join(tmpdir, 'src'))
        assert len(target.exported_includes) == 1
        assert target.exported_includes[0] == src_dir
    finally:
        shutil.rmtree(tmpdir)


def test_export_include_without_includes_root():
    tmpdir = make_temp_lib_tree()
    try:
        target = make_mock_target(tmpdir)
        package.export_include(target, 'src/mylib', build_dir=False,
                               as_includes_root=False)

        mylib_dir = normalized_path(os.path.join(tmpdir, 'src', 'mylib'))
        assert target.exported_includes == [mylib_dir]
        assert target.includes_root == ('', '', '')
    finally:
        shutil.rmtree(tmpdir)


def test_export_include_nonexistent_path_returns_false():
    tmpdir = make_temp_lib_tree()
    try:
        target = make_mock_target(tmpdir)
        result = package.export_include(target, 'nonexistent/path', build_dir=False,
                                        as_includes_root=True)
        assert result is False
        assert target.exported_includes == []
        assert target.includes_root == ('', '', '')
    finally:
        shutil.rmtree(tmpdir)


def test_export_include_no_duplicate_includes():
    tmpdir = make_temp_lib_tree()
    try:
        target = make_mock_target(tmpdir)
        package.export_include(target, 'src/mylib', build_dir=False,
                               as_includes_root=True)
        package.export_include(target, 'src/mylib', build_dir=False,
                               as_includes_root=True)
        assert len(target.exported_includes) == 1
    finally:
        shutil.rmtree(tmpdir)


# -- string alias --

def test_export_include_string_alias_sets_includes_root():
    # as_includes_root='mylib' sets the alias to 'mylib', not to the basename of the path
    tmpdir = make_temp_flat_lib_tree()
    try:
        target = make_mock_target(tmpdir)
        result = package.export_include(target, 'src', build_dir=False,
                                        as_includes_root='mylib')
        assert result is True

        parent_dir = normalized_path(tmpdir)
        src_dir = normalized_path(os.path.join(tmpdir, 'src'))
        assert target.includes_root == (parent_dir, src_dir, 'mylib')
    finally:
        shutil.rmtree(tmpdir)


def test_export_include_string_alias_exports_parent():
    tmpdir = make_temp_flat_lib_tree()
    try:
        target = make_mock_target(tmpdir)
        package.export_include(target, 'src', build_dir=False,
                               as_includes_root='mylib')

        parent_dir = normalized_path(tmpdir)
        assert target.exported_includes == [parent_dir]
    finally:
        shutil.rmtree(tmpdir)


def test_export_include_bool_true_uses_basename_as_alias():
    tmpdir = make_temp_lib_tree()
    try:
        target = make_mock_target(tmpdir)
        package.export_include(target, 'src/mylib', build_dir=False,
                               as_includes_root=True)

        src_dir = normalized_path(os.path.join(tmpdir, 'src'))
        mylib_dir = normalized_path(os.path.join(tmpdir, 'src', 'mylib'))
        assert target.includes_root == (src_dir, mylib_dir, 'mylib')
    finally:
        shutil.rmtree(tmpdir)


# -- cmake defines --

def test_cmake_defines_uses_parent_include_path():
    tmpdir = make_temp_lib_tree()
    try:
        target = make_mock_target(tmpdir)
        package.export_include(target, 'src/mylib', build_dir=False,
                               as_includes_root=True)

        dep = make_mock_dep(target)
        includes_var, cmake_text = _get_dependency_cmake_defines(dep)

        src_dir = normalized_path(os.path.join(tmpdir, 'src'))
        assert includes_var == '${TestLib_INCLUDES}'
        assert src_dir in cmake_text
        mylib_dir = normalized_path(os.path.join(tmpdir, 'src', 'mylib'))
        assert f'"{mylib_dir}"' not in cmake_text
    finally:
        shutil.rmtree(tmpdir)


def test_cmake_defines_without_includes_root():
    tmpdir = make_temp_lib_tree()
    try:
        target = make_mock_target(tmpdir)
        package.export_include(target, 'src/mylib', build_dir=False,
                               as_includes_root=False)

        dep = make_mock_dep(target)
        _, cmake_text = _get_dependency_cmake_defines(dep)

        mylib_dir = normalized_path(os.path.join(tmpdir, 'src', 'mylib'))
        assert f'"{mylib_dir}"' in cmake_text
    finally:
        shutil.rmtree(tmpdir)


# -- papa deploy: _append_includes --

def test_append_includes_deploys_to_include_foldername():
    tmpdir = make_temp_lib_tree()
    try:
        target = make_mock_target(tmpdir)
        package.export_include(target, 'src/mylib', build_dir=False,
                               as_includes_root=True)

        target.config.verbose = False
        target.config.print = False

        deploy_dir = os.path.join(tmpdir, 'deploy', 'TestLib')
        os.makedirs(deploy_dir, exist_ok=True)

        descr = ['P TestLib']
        src_dir = normalized_path(os.path.join(tmpdir, 'src'))
        includes = [(target, src_dir)]

        _append_includes(target, deploy_dir, False, descr, includes)

        deployed_mylib_h = os.path.join(deploy_dir, 'include', 'mylib', 'mylib.h')
        deployed_internal_h = os.path.join(deploy_dir, 'include', 'mylib', 'internal.h')
        assert os.path.isfile(deployed_mylib_h), f'Expected {deployed_mylib_h} to exist'
        assert os.path.isfile(deployed_internal_h), f'Expected {deployed_internal_h} to exist'

        # the record is 'I include', not 'I include/src' or 'I include/mylib'
        assert 'I include' in descr
        assert not any('I include/' in d for d in descr), \
            f'Expected no I include/<subpath> entries, got {descr}'
    finally:
        shutil.rmtree(tmpdir)


def test_append_includes_without_includes_root_uses_subpath():
    tmpdir = make_temp_lib_tree()
    try:
        target = make_mock_target(tmpdir)
        package.export_include(target, 'src/mylib', build_dir=False,
                               as_includes_root=False)

        target.config.verbose = False
        target.config.print = False

        deploy_dir = os.path.join(tmpdir, 'deploy', 'TestLib')
        os.makedirs(deploy_dir, exist_ok=True)

        descr = ['P TestLib']
        mylib_dir = normalized_path(os.path.join(tmpdir, 'src', 'mylib'))
        includes = [(target, mylib_dir)]

        _append_includes(target, deploy_dir, False, descr, includes)

        assert 'I include/mylib' in descr
    finally:
        shutil.rmtree(tmpdir)


def test_append_includes_string_alias_remaps_dirname():
    tmpdir = make_temp_flat_lib_tree()
    try:
        target = make_mock_target(tmpdir)
        package.export_include(target, 'src', build_dir=False,
                               as_includes_root='mylib')

        target.config.verbose = False
        target.config.print = False

        deploy_dir = os.path.join(tmpdir, 'deploy', 'TestLib')
        os.makedirs(deploy_dir, exist_ok=True)

        descr = ['P TestLib']
        parent_dir = normalized_path(tmpdir)
        includes = [(target, parent_dir)]

        _append_includes(target, deploy_dir, False, descr, includes)

        deployed_mylib_h = os.path.join(deploy_dir, 'include', 'mylib', 'mylib.h')
        deployed_internal_h = os.path.join(deploy_dir, 'include', 'mylib', 'internal.h')
        assert os.path.isfile(deployed_mylib_h), f'Expected {deployed_mylib_h} to exist'
        assert os.path.isfile(deployed_internal_h), f'Expected {deployed_internal_h} to exist'

        assert not os.path.exists(os.path.join(deploy_dir, 'include', 'src')), \
            'Should NOT have include/src/ directory'

        assert 'I include' in descr
        assert not any('I include/' in d for d in descr), \
            f'Expected no I include/<subpath> entries, got {descr}'
    finally:
        shutil.rmtree(tmpdir)


# -- full papa deploy --

def test_papa_deploy_writes_correct_papa_txt():
    tmpdir = make_temp_lib_tree()
    try:
        target = make_mock_target(tmpdir)
        package.export_include(target, 'src/mylib', build_dir=False,
                               as_includes_root=True)

        target.config.verbose = False
        target.config.print = False
        target.config.test = False
        target.children.return_value = []
        target.is_current_target.return_value = False

        deploy_dir = os.path.join(tmpdir, 'deploy', 'TestLib')
        os.makedirs(deploy_dir, exist_ok=True)

        papa_deploy_to(target, deploy_dir,
                       r_includes=False, r_dylibs=False,
                       r_syslibs=False, r_assets=False)

        papa_file = os.path.join(deploy_dir, 'papa.txt')
        assert os.path.isfile(papa_file), 'papa.txt should exist after deploy'

        with open(papa_file, 'r') as f:
            content = f.read()
        lines = content.strip().split('\n')

        assert lines[0] == 'P TestLib'

        include_lines = [l for l in lines if l.startswith('I ')]
        assert 'I include' in include_lines, \
            f'Expected "I include" in papa.txt, got: {include_lines}'

        assert not any('I include/' in l for l in include_lines), \
            f'Expected no I include/<subpath> entries, got: {include_lines}'
    finally:
        shutil.rmtree(tmpdir)


def test_papa_txt_parsed_correctly_with_includes_root():
    tmpdir = make_temp_lib_tree()
    try:
        target = make_mock_target(tmpdir)
        package.export_include(target, 'src/mylib', build_dir=False,
                               as_includes_root=True)

        target.config.verbose = False
        target.config.print = False
        target.config.test = False
        target.children.return_value = []
        target.is_current_target.return_value = False

        deploy_dir = os.path.join(tmpdir, 'deploy', 'TestLib')
        os.makedirs(deploy_dir, exist_ok=True)

        papa_deploy_to(target, deploy_dir,
                       r_includes=False, r_dylibs=False,
                       r_syslibs=False, r_assets=False)

        papa_file = os.path.join(deploy_dir, 'papa.txt')
        papa = PapaFileInfo(papa_file)

        assert papa.project_name == 'TestLib'
        assert len(papa.includes) == 1
        assert papa.includes[0].endswith('include'), \
            f'Expected include path ending with "include", got: {papa.includes[0]}'

        expected_include = normalized_path(os.path.join(deploy_dir, 'include'))
        assert normalized_path(papa.includes[0]) == expected_include
    finally:
        shutil.rmtree(tmpdir)


def test_deployed_headers_are_accessible_via_includes_root():
    tmpdir = make_temp_lib_tree()
    try:
        target = make_mock_target(tmpdir)
        package.export_include(target, 'src/mylib', build_dir=False,
                               as_includes_root=True)

        target.config.verbose = False
        target.config.print = False
        target.config.test = False
        target.children.return_value = []
        target.is_current_target.return_value = False

        deploy_dir = os.path.join(tmpdir, 'deploy', 'TestLib')
        os.makedirs(deploy_dir, exist_ok=True)

        papa_deploy_to(target, deploy_dir,
                       r_includes=False, r_dylibs=False,
                       r_syslibs=False, r_assets=False)

        # the layout #include <mylib/mylib.h> expects
        include_dir = os.path.join(deploy_dir, 'include')
        assert os.path.isdir(os.path.join(include_dir, 'mylib')), \
            'Expected include/mylib/ directory'
        assert os.path.isfile(os.path.join(include_dir, 'mylib', 'mylib.h')), \
            'Expected include/mylib/mylib.h'
        assert os.path.isfile(os.path.join(include_dir, 'mylib', 'internal.h')), \
            'Expected include/mylib/internal.h'

        assert not os.path.exists(os.path.join(include_dir, 'src')), \
            'Should NOT have include/src/ directory'
    finally:
        shutil.rmtree(tmpdir)
