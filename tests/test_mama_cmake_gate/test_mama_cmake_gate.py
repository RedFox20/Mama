"""Pins which deps get a `<src_dir>/mama.cmake` proxy written into their source tree."""
import os
import mama.dependency_chain as dc
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
