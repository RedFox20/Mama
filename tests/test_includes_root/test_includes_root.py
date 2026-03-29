"""
Tests for the `as_includes_root` feature of export_include.

When `export_include('src/mylib', as_includes_root=True)` is called:
  1. The parent dir ('src/') becomes the exported include path
  2. target.includes_root is set to (parent_path, original_path)
  3. CMake deps file references the parent path
  4. Papa deploy copies src/mylib/ -> deploy/include/mylib/
  5. papa.txt contains 'I include' (not 'I include/src')
"""
import os
import tempfile
import shutil
from unittest.mock import Mock

from mama.util import normalized_path
import mama.package as package
from mama.papa_deploy import _append_includes, papa_deploy_to, PapaFileInfo
from mama.dependency_chain import _get_dependency_cmake_defines, _get_cmake_path_list


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_mock_target(source_dir, build_dir=None):
    """Create a mock BuildTarget rooted at source_dir."""
    target = Mock()
    target.source_dir.return_value = normalized_path(source_dir)
    target.build_dir.return_value = normalized_path(build_dir or os.path.join(source_dir, 'build'))
    target.exported_includes = []
    target.exported_libs = []
    target.exported_syslibs = []
    target.exported_assets = []
    target.includes_root = ('', '')
    target.include_glob_filter = ['.h', '.hpp', '.hxx', '.hh']
    target.name = 'TestLib'
    return target


def make_mock_dep(target, name='TestLib', children=None):
    """Create a mock BuildDependency wrapping a target."""
    dep = Mock()
    dep.name = name
    dep.target = target
    dep.children = children or []
    dep.get_children.return_value = dep.children
    return dep


def make_temp_lib_tree():
    """
    Create a temp directory tree:
        tmpdir/
          src/
            mylib/
              mylib.h
              internal.h
    Returns (tmpdir, src_mylib_path)
    """
    tmpdir = tempfile.mkdtemp(prefix='mama_test_')
    mylib = os.path.join(tmpdir, 'src', 'mylib')
    os.makedirs(mylib)
    # create header files
    with open(os.path.join(mylib, 'mylib.h'), 'w') as f:
        f.write('#pragma once\nint mylib_func();\n')
    with open(os.path.join(mylib, 'internal.h'), 'w') as f:
        f.write('#pragma once\nint internal_func();\n')
    return tmpdir


# ===========================================================================
# 1. export_include with as_includes_root sets correct state
# ===========================================================================

def test_export_include_sets_includes_root():
    """includes_root tuple should be (parent_path, original_path)."""
    tmpdir = make_temp_lib_tree()
    try:
        target = make_mock_target(tmpdir)
        result = package.export_include(target, 'src/mylib', build_dir=False,
                                        as_includes_root=True)
        assert result is True

        src_dir = normalized_path(os.path.join(tmpdir, 'src'))
        mylib_dir = normalized_path(os.path.join(tmpdir, 'src', 'mylib'))
        assert target.includes_root == (src_dir, mylib_dir)
    finally:
        shutil.rmtree(tmpdir)


def test_export_include_adds_parent_to_exported_includes():
    """exported_includes should contain the parent dir, not the subfolder."""
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
    """Without as_includes_root, the path itself is added and includes_root stays empty."""
    tmpdir = make_temp_lib_tree()
    try:
        target = make_mock_target(tmpdir)
        package.export_include(target, 'src/mylib', build_dir=False,
                               as_includes_root=False)

        mylib_dir = normalized_path(os.path.join(tmpdir, 'src', 'mylib'))
        assert target.exported_includes == [mylib_dir]
        assert target.includes_root == ('', '')
    finally:
        shutil.rmtree(tmpdir)


def test_export_include_nonexistent_path_returns_false():
    """export_include should return False for paths that don't exist."""
    tmpdir = make_temp_lib_tree()
    try:
        target = make_mock_target(tmpdir)
        result = package.export_include(target, 'nonexistent/path', build_dir=False,
                                        as_includes_root=True)
        assert result is False
        assert target.exported_includes == []
        assert target.includes_root == ('', '')
    finally:
        shutil.rmtree(tmpdir)


def test_export_include_no_duplicate_includes():
    """Calling export_include twice with same path should not duplicate."""
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


# ===========================================================================
# 2. CMake dependency generation uses the parent include path
# ===========================================================================

def test_cmake_defines_uses_parent_include_path():
    """The cmake defines should reference the parent (src/) not src/mylib/."""
    tmpdir = make_temp_lib_tree()
    try:
        target = make_mock_target(tmpdir)
        package.export_include(target, 'src/mylib', build_dir=False,
                               as_includes_root=True)

        dep = make_mock_dep(target)
        includes_var, cmake_text = _get_dependency_cmake_defines(dep)

        src_dir = normalized_path(os.path.join(tmpdir, 'src'))
        assert includes_var == '${TestLib_INCLUDES}'
        # the cmake set() should contain the parent path
        assert src_dir in cmake_text
        # and should NOT contain the mylib subfolder path directly in includes
        mylib_dir = normalized_path(os.path.join(tmpdir, 'src', 'mylib'))
        assert f'"{mylib_dir}"' not in cmake_text
    finally:
        shutil.rmtree(tmpdir)


def test_cmake_defines_without_includes_root():
    """Without as_includes_root, cmake should reference the exact subfolder."""
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


# ===========================================================================
# 3. Papa deploy: _append_includes copies to correct location
# ===========================================================================

def test_append_includes_deploys_to_include_foldername():
    """With includes_root, headers from src/mylib/ should be copied to deploy/include/mylib/."""
    tmpdir = make_temp_lib_tree()
    try:
        target = make_mock_target(tmpdir)
        package.export_include(target, 'src/mylib', build_dir=False,
                               as_includes_root=True)

        # set up config for _append_includes
        target.config.verbose = False
        target.config.print = False

        deploy_dir = os.path.join(tmpdir, 'deploy', 'TestLib')
        os.makedirs(deploy_dir, exist_ok=True)

        descr = ['P TestLib']
        src_dir = normalized_path(os.path.join(tmpdir, 'src'))
        includes = [(target, src_dir)]

        _append_includes(target, deploy_dir, False, descr, includes)

        # Check that headers were copied to deploy/TestLib/include/mylib/
        deployed_mylib_h = os.path.join(deploy_dir, 'include', 'mylib', 'mylib.h')
        deployed_internal_h = os.path.join(deploy_dir, 'include', 'mylib', 'internal.h')
        assert os.path.isfile(deployed_mylib_h), f'Expected {deployed_mylib_h} to exist'
        assert os.path.isfile(deployed_internal_h), f'Expected {deployed_internal_h} to exist'

        # Check that 'I include' is in descr (not 'I include/src' or 'I include/mylib')
        assert 'I include' in descr
        assert not any('I include/' in d for d in descr), \
            f'Expected no I include/<subpath> entries, got {descr}'
    finally:
        shutil.rmtree(tmpdir)


def test_append_includes_without_includes_root_uses_subpath():
    """Without includes_root, includes from 'mylib/' get deployed as include/mylib/."""
    tmpdir = make_temp_lib_tree()
    try:
        target = make_mock_target(tmpdir)
        # export without as_includes_root
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

        # Without includes_root, it should use include/mylib in descr
        assert 'I include/mylib' in descr
    finally:
        shutil.rmtree(tmpdir)


# ===========================================================================
# 4. Full papa deploy writes correct papa.txt
# ===========================================================================

def test_papa_deploy_writes_correct_papa_txt():
    """Full papa_deploy_to should produce papa.txt with 'I include' for includes_root."""
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

        # First line is project name
        assert lines[0] == 'P TestLib'

        # Should have 'I include' line
        include_lines = [l for l in lines if l.startswith('I ')]
        assert 'I include' in include_lines, \
            f'Expected "I include" in papa.txt, got: {include_lines}'

        # Should NOT have 'I include/src' or similar subpath entries
        assert not any('I include/' in l for l in include_lines), \
            f'Expected no I include/<subpath> entries, got: {include_lines}'
    finally:
        shutil.rmtree(tmpdir)


def test_papa_txt_parsed_correctly_with_includes_root():
    """PapaFileInfo should parse a papa.txt generated with includes_root."""
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

        # The resolved include path should point to deploy/TestLib/include
        expected_include = normalized_path(os.path.join(deploy_dir, 'include'))
        assert normalized_path(papa.includes[0]) == expected_include
    finally:
        shutil.rmtree(tmpdir)


def test_deployed_headers_are_accessible_via_includes_root():
    """After deploy, headers should exist at include/mylib/*.h (the clean include path)."""
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

        # Verify the include structure matches what #include <mylib/mylib.h> expects
        include_dir = os.path.join(deploy_dir, 'include')
        assert os.path.isdir(os.path.join(include_dir, 'mylib')), \
            'Expected include/mylib/ directory'
        assert os.path.isfile(os.path.join(include_dir, 'mylib', 'mylib.h')), \
            'Expected include/mylib/mylib.h'
        assert os.path.isfile(os.path.join(include_dir, 'mylib', 'internal.h')), \
            'Expected include/mylib/internal.h'

        # Verify no stale src/ prefix directory
        assert not os.path.exists(os.path.join(include_dir, 'src')), \
            'Should NOT have include/src/ directory'
    finally:
        shutil.rmtree(tmpdir)
