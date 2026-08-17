"""Pins which deps get a `<src_dir>/mama.cmake` proxy written into their source tree."""
import os
from unittest.mock import patch
import pytest
import mama.dependency_chain as dc
from mama.utils.errors import BuildError
from testutils import make_mock_local_dep, write_files

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


def test_an_uppercase_include_gets_the_proxy(tmp_path):
    # every cmake command name is case-insensitive, so INCLUDE(mama.cmake) is the same command
    assert dc._needs_mama_cmake(_includes_proxy(_dep(tmp_path, 'leaf'), 'INCLUDE(Mama.cmake)\n'))


def test_a_locale_encoded_cmakelists_reads_without_ending_the_run(tmp_path):
    # cmake configures an 8-bit-clean file, so a Latin-1 comment must not raise UnicodeDecodeError
    dep = _dep(tmp_path, 'leaf')
    open(dep.cmakelists_path(), 'wb').write('# caf\xe9\ninclude(mama.cmake)\n'.encode('latin-1'))
    assert dc._needs_mama_cmake(dep)


def test_a_nested_cmakelists_gets_the_proxy_beside_it(tmp_path):
    # cmake configures the dir of the CMakeLists.txt, and a bare include resolves against that dir
    dep = _dep(tmp_path, 'leaf')
    dep.target.cmake_lists_path = 'cmake/CMakeLists.txt'
    write_files(dep.src_dir, {'cmake/CMakeLists.txt': 'include(mama.cmake)\n'})
    dc._save_cmake_files(dep)
    assert os.path.exists(f'{dep.src_dir}/cmake/mama.cmake')
    assert not os.path.exists(f'{dep.src_dir}/mama.cmake')


def test_an_include_split_over_two_lines_gets_the_proxy(tmp_path):
    # a cmake command takes whitespace-separated arguments, so a newline inside the parens is valid
    assert dc._needs_mama_cmake(_includes_proxy(_dep(tmp_path, 'leaf'), 'include(\n  mama.cmake)\n'))


def test_a_trailing_comment_that_names_the_proxy_gets_none(tmp_path):
    dep = _includes_proxy(_dep(tmp_path, 'leaf'), 'include(other.cmake) # not mama.cmake\n')
    assert not dc._needs_mama_cmake(dep)


def test_a_bracket_comment_that_names_the_proxy_gets_none(tmp_path):
    dep = _includes_proxy(_dep(tmp_path, 'leaf'), '#[[\ninclude(mama.cmake)\n]]\n')
    assert not dc._needs_mama_cmake(dep)


def test_an_equals_bracket_comment_that_names_the_proxy_gets_none(tmp_path):
    # a cmake bracket comment takes any number of equals signs between its brackets
    dep = _includes_proxy(_dep(tmp_path, 'leaf'), '#[==[\ninclude(mama.cmake)\n]==]\n')
    assert not dc._needs_mama_cmake(dep)


def test_an_env_variable_keeps_the_default_location(tmp_path):
    dep = _includes_proxy(_dep(tmp_path, 'leaf'), 'include("$ENV{MY_DIR}/mama.cmake")\n')
    dc._save_cmake_files(dep)
    assert os.path.exists(f'{dep.src_dir}/mama.cmake')


def test_a_rewritten_cmakelists_rereads_at_the_same_path(tmp_path):
    # a configure() hook can rewrite the file in place, so the cached answer keys on the write time too
    dep = _dep(tmp_path, 'leaf')
    assert not dep.cmakelists_includes_mama_cmake()
    _includes_proxy(dep)
    os.utime(dep.cmakelists_path(), (2_000_000_000, 2_000_000_000))  # pin a distinct write time
    assert dep.cmakelists_includes_mama_cmake()


def test_the_proxy_follows_the_path_the_include_names(tmp_path):
    # a nested CMakeLists.txt can ask for the proxy of the dir above it
    dep = _dep(tmp_path, 'leaf')
    dep.target.cmake_lists_path = 'cmake/CMakeLists.txt'
    write_files(dep.src_dir, {'cmake/CMakeLists.txt': 'include("${CMAKE_CURRENT_LIST_DIR}/../mama.cmake")\n'})
    dc._save_cmake_files(dep)
    assert os.path.exists(f'{dep.src_dir}/mama.cmake')
    assert not os.path.exists(f'{dep.src_dir}/cmake/mama.cmake')


def test_a_variable_mama_cannot_expand_keeps_the_default_location(tmp_path):
    # an unknown variable must not invent a path, and the guard must not fail a run over it
    dep = _includes_proxy(_dep(tmp_path, 'leaf'), 'include("${MY_DIR}/mama.cmake")\n')
    dc._save_cmake_files(dep)
    assert os.path.exists(f'{dep.src_dir}/mama.cmake')


def test_a_configure_hook_that_moves_the_cmakelists_still_gets_a_proxy(tmp_path):
    # _save_cmake_files runs before configure(), so the cmake configure step writes the proxy again
    dep = _dep(tmp_path, 'leaf')
    dc._save_cmake_files(dep)
    dep.target.cmake_lists_path = 'cmake/CMakeLists.txt'
    write_files(dep.src_dir, {'cmake/CMakeLists.txt': 'include(mama.cmake)\n'})
    with patch('mama.build_target.cmake.inject_env'), patch('mama.build_target.cmake.run_config'):
        dep.target._cmake_configure_step()
    assert os.path.exists(f'{dep.src_dir}/cmake/mama.cmake')


def test_a_missing_proxy_the_cmakelists_includes_names_the_dep(tmp_path):
    # cmake would report a missing header of an unrelated project minutes later, and never name mama
    dep = _includes_proxy(_dep(tmp_path, 'leaf'))
    with patch('mama.dependency_chain._save_mama_cmake'):
        with pytest.raises(BuildError, match='includes mama.cmake'):
            dc._save_cmake_files(dep)
