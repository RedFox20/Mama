"""Pins which deps get a `<src_dir>/mama.cmake` proxy written into their source tree."""
import os
from unittest.mock import patch
import pytest
import mama.dependency_chain as dc
from mama.utils.errors import BuildError
from testutils import make_mock_local_dep

_MAMAFILE = 'import mama\nclass Gated(mama.BuildTarget):\n    pass\n'


def _dep(tmp_path, name, files=('mamafile.py', 'CMakeLists.txt'), children=()):
    src = tmp_path / name
    src.mkdir()
    for f in files: (src / f).write_text(_MAMAFILE if f == 'mamafile.py' else '')
    dep = make_mock_local_dep(tmp_path, src, name=name)
    dep.children = list(children)
    return dep


def _trunk(tmp_path, files=('mamafile.py', 'CMakeLists.txt')):
    return _dep(tmp_path, 'trunk', files, children=[_dep(tmp_path, 'child')])


def test_a_trunk_with_both_files_gets_the_proxy(tmp_path):
    dep = _trunk(tmp_path)
    dc._save_cmake_files(dep)
    assert os.path.exists(f'{dep.src_dir}/mama.cmake')


def test_a_leaf_gets_no_proxy(tmp_path):
    # a leaf exports no dependency includes or libs, so the proxy would only pollute its source tree
    assert not dc._needs_mama_cmake(_dep(tmp_path, 'leaf'))


def test_a_trunk_with_no_cmakelists_gets_no_proxy(tmp_path):
    assert not dc._needs_mama_cmake(_trunk(tmp_path, files=('mamafile.py',)))


def test_a_trunk_with_no_mamafile_gets_no_proxy(tmp_path):
    assert not dc._needs_mama_cmake(_trunk(tmp_path, files=('CMakeLists.txt',)))


def test_an_artifactory_package_gets_no_proxy(tmp_path):
    dep = _trunk(tmp_path)
    dep.src_dir = ''  # a fetched package has no source dir to write into
    assert not dc._needs_mama_cmake(dep)


def test_a_skipped_proxy_still_leaves_the_dependency_exports(tmp_path):
    # the build dir file is what a parent's cmake reads, and it is written for every dep
    dep = _dep(tmp_path, 'leaf')
    dc._save_cmake_files(dep)
    assert os.path.exists(f'{dep.build_dir}/mama-dependencies.cmake')
    assert not os.path.exists(f'{dep.src_dir}/mama.cmake')


# --- the consumer decides: a CMakeLists.txt that includes the proxy always gets one ---------------

def _includes_proxy(dep, line='include(mama.cmake)\n'):
    open(dep.cmakelists_path(), 'w').write(f'project(Test)\n{line}')
    dep._includes_mama_cmake = None  # the answer is cached, and this test writes the file after load
    return dep


def test_a_leaf_that_includes_the_proxy_gets_one(tmp_path):
    dep = _includes_proxy(_dep(tmp_path, 'leaf'))
    dc._save_cmake_files(dep)
    assert os.path.exists(f'{dep.src_dir}/mama.cmake')


def test_a_leaf_with_no_mamafile_that_includes_the_proxy_gets_one(tmp_path):
    dep = _includes_proxy(_dep(tmp_path, 'leaf', files=('CMakeLists.txt',)))
    dc._save_cmake_files(dep)
    assert os.path.exists(f'{dep.src_dir}/mama.cmake')


def test_a_hash_commented_include_gets_no_proxy(tmp_path):
    assert not dc._needs_mama_cmake(_includes_proxy(_dep(tmp_path, 'leaf'), '# include(mama.cmake)\n'))


def test_a_path_qualified_include_gets_the_proxy(tmp_path):
    dep = _includes_proxy(_dep(tmp_path, 'leaf'), 'include("${CMAKE_CURRENT_SOURCE_DIR}/mama.cmake")\n')
    assert dc._needs_mama_cmake(dep)


def test_a_proxy_already_on_disk_survives(tmp_path):
    dep = _includes_proxy(_dep(tmp_path, 'leaf'))
    dc._save_cmake_files(dep)
    dc._save_cmake_files(dep)
    assert os.path.exists(f'{dep.src_dir}/mama.cmake')


def test_a_missing_proxy_the_cmakelists_includes_names_the_dep(tmp_path):
    # cmake would report a missing header of an unrelated project minutes later, and never name mama
    dep = _includes_proxy(_dep(tmp_path, 'leaf'))
    with patch('mama.dependency_chain._save_mama_cmake'):
        with pytest.raises(BuildError, match='includes mama.cmake'):
            dc._save_cmake_files(dep)
