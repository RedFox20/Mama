"""Pins which deps get a `<src_dir>/mama.cmake` proxy written into their source tree."""
import os
from unittest.mock import patch
import pytest
import mama.dependency_chain as dc
from mama.build_dependency import has_unknown_cmake_var
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


def test_a_trailing_comment_that_names_the_proxy_gets_none(tmp_path):
    dep = _includes_proxy(_dep(tmp_path, 'leaf'), 'include(other.cmake) # not mama.cmake\n')
    assert not dc._needs_mama_cmake(dep)


@pytest.mark.parametrize('written, proxy', [
    ('include(\n  mama.cmake)\n',          False),   # the argument must end on the line the command starts
    ('include( # written by mama\n  mama.cmake)\n', False),
    ('#[[\ninclude(mama.cmake)\n]]\n',     True),    # a bracket comment hides no line from the scan
    ('#[==[\ninclude(mama.cmake)\n]==]\n', True),
], ids=['split', 'comment-in-call', 'bracket', 'equals-bracket'])
def test_mama_reads_a_line_at_a_time_and_not_the_cmake_language(tmp_path, written, proxy):
    # a miss ends with cmake naming mama.cmake, and an extra proxy is a file that nothing reads
    assert dc._needs_mama_cmake(_includes_proxy(_dep(tmp_path, 'leaf'), written)) is proxy


def test_an_env_variable_keeps_the_default_location(tmp_path):
    dep = _includes_proxy(_dep(tmp_path, 'leaf'), 'include("$ENV{MY_DIR}/mama.cmake")\n')
    dc._save_cmake_files(dep)
    assert os.path.exists(f'{dep.src_dir}/mama.cmake')


def test_a_rewritten_cmakelists_rereads_at_the_same_path(tmp_path):
    # a configure() hook can rewrite the file in place with no change a stat can see, so nothing caches
    dep = _dep(tmp_path, 'leaf')
    assert not dc._needs_mama_cmake(dep)
    _includes_proxy(dep)
    os.utime(dep.cmakelists_path(), (2_000_000_000, 2_000_000_000))  # a preserved timestamp still re-reads
    assert dc._needs_mama_cmake(dep)


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


def test_a_conditional_include_gets_a_proxy_per_branch(tmp_path):
    # only cmake knows which branch runs, so every named path gets a proxy
    dep = _includes_proxy(_dep(tmp_path, 'leaf'),
                          'if(WIN32)\n  include(win/mama.cmake)\nelse()\n  include(mama.cmake)\nendif()\n')
    dc._save_cmake_files(dep)
    assert os.path.exists(f'{dep.src_dir}/win/mama.cmake')
    assert os.path.exists(f'{dep.src_dir}/mama.cmake')


def test_a_module_whose_name_ends_in_mama_cmake_is_left_alone(tmp_path):
    # a write would replace a real module, so the basename has to match exactly
    dep = _includes_proxy(_dep(tmp_path, 'leaf'), 'include(grandmama.cmake)\n')
    write_files(dep.src_dir, {'grandmama.cmake': 'set(REAL_MODULE 1)\n'})
    assert not dc._needs_mama_cmake(dep)
    dc._save_cmake_files(dep)
    assert 'REAL_MODULE' in open(f'{dep.src_dir}/grandmama.cmake').read()


def test_an_include_inside_a_quoted_argument_gets_no_proxy(tmp_path):
    dep = _includes_proxy(_dep(tmp_path, 'leaf'), 'message("include(mama.cmake)")\n')
    assert not dc._needs_mama_cmake(dep)


def test_an_include_inside_a_bracket_argument_gets_no_proxy(tmp_path):
    dep = _includes_proxy(_dep(tmp_path, 'leaf'), 'message([[include(mama.cmake)]])\n')
    assert not dc._needs_mama_cmake(dep)


def test_an_include_named_inside_a_message_string_gets_no_proxy(tmp_path):
    # a quoted argument may run over lines and hold parens, and cmake runs no command inside one
    dep = _includes_proxy(_dep(tmp_path, 'leaf'), 'message(FATAL_ERROR "run mama, then include(mama.cmake)")\n')
    assert not dc._needs_mama_cmake(dep)


def test_a_hash_inside_a_quoted_argument_opens_no_comment(tmp_path):
    # a stripped `#` used to eat the closing quote, which flipped every later include of the file
    dep = _includes_proxy(_dep(tmp_path, 'leaf'), 'message(STATUS "build #${N}")\ninclude(mama.cmake)\n')
    assert dc._needs_mama_cmake(dep)


def test_a_quoted_include_path_may_hold_a_hash(tmp_path):
    # cmake reads a `#` inside a quoted argument as text, so it opens no comment there
    dep = _includes_proxy(_dep(tmp_path, 'leaf'), 'include("generated #1/mama.cmake")\n')
    dc._save_cmake_files(dep)
    assert os.path.exists(f'{dep.src_dir}/generated #1/mama.cmake')


def test_a_quoted_include_path_may_hold_parens(tmp_path):
    dep = _includes_proxy(_dep(tmp_path, 'leaf'), 'include("Program Files (x86)/mama.cmake")\n')
    dc._save_cmake_files(dep)
    assert os.path.exists(f'{dep.src_dir}/Program Files (x86)/mama.cmake')


def test_an_include_nested_in_another_command_gets_no_proxy(tmp_path):
    # cmake hands `include`, `(`, `mama.cmake` and `)` to set() as text, and runs no include
    dep = _includes_proxy(_dep(tmp_path, 'leaf'), 'set(X include(mama.cmake))\n')
    assert not dc._needs_mama_cmake(dep)


def test_a_quoted_include_path_may_hold_a_space(tmp_path):
    dep = _includes_proxy(_dep(tmp_path, 'leaf'), 'include("my cmake/mama.cmake")\n')
    dc._save_cmake_files(dep)
    assert os.path.exists(f'{dep.src_dir}/my cmake/mama.cmake')


def test_a_symlink_that_leads_out_of_the_source_dir_writes_nothing(tmp_path):
    outside = tmp_path / 'outside'; outside.mkdir()
    dep = _includes_proxy(_dep(tmp_path, 'leaf'), 'include(link/mama.cmake)\n')
    try: os.symlink(outside, f'{dep.src_dir}/link', target_is_directory=True)
    except (OSError, NotImplementedError): pytest.skip('this host does not allow a symlink')
    dc._save_cmake_files(dep)
    assert not os.path.exists(f'{outside}/mama.cmake')


def test_an_include_that_points_outside_the_source_dir_writes_nothing(tmp_path):
    # a proxy written above the source tree could replace a file mama never generated
    dep = _includes_proxy(_dep(tmp_path, 'leaf'), 'include(../../mama.cmake)\n')
    assert not dc._needs_mama_cmake(dep)
    dc._save_cmake_files(dep)
    assert not os.path.exists(f'{tmp_path}/mama.cmake')


def test_an_include_above_a_nested_cmakelists_stays_inside_the_source_dir(tmp_path):
    dep = _dep(tmp_path, 'leaf')
    dep.target.cmake_lists_path = 'cmake/CMakeLists.txt'
    write_files(dep.src_dir, {'cmake/CMakeLists.txt': 'include(../mama.cmake)\n'})
    dc._save_cmake_files(dep)
    assert os.path.exists(f'{dep.src_dir}/mama.cmake')


def test_a_missing_proxy_the_cmakelists_includes_names_the_dep(tmp_path):
    # cmake would report a missing header of an unrelated project minutes later, and never name mama
    dep = _includes_proxy(_dep(tmp_path, 'leaf'))
    with patch('mama.dependency_chain._save_mama_cmake'):
        with pytest.raises(BuildError, match='wrote no '):
            dc._save_cmake_files(dep)


def _subdir_dep(tmp_path, sub_line, root_line='add_subdirectory(src)\n'):
    dep = _includes_proxy(_dep(tmp_path, 'leaf'), root_line)
    write_files(dep.src_dir, {'src/CMakeLists.txt': sub_line})
    return dep


def test_a_leaf_whose_subdirectory_includes_the_proxy_gets_one(tmp_path):
    # cmake reads src/CMakeLists.txt through add_subdirectory, so an include there names the proxy too
    dep = _subdir_dep(tmp_path, 'include(${CMAKE_SOURCE_DIR}/mama.cmake)\n')
    dc._save_cmake_files(dep)
    assert os.path.exists(f'{dep.src_dir}/mama.cmake')


def test_a_subdirectory_bare_include_gets_the_proxy_beside_it(tmp_path):
    # a bare include resolves against the dir of the file that names it, never the top dir
    dep = _subdir_dep(tmp_path, 'include(mama.cmake)\n')
    dc._save_cmake_files(dep)
    assert os.path.exists(f'{dep.src_dir}/src/mama.cmake')
    assert not os.path.exists(f'{dep.src_dir}/mama.cmake')


def test_a_root_and_a_subdirectory_that_both_include_get_a_proxy_each(tmp_path):
    # cmake resolves each relative include against its own dir, so one proxy cannot serve both
    dep = _subdir_dep(tmp_path, 'include(mama.cmake)\n', 'include(mama.cmake)\nadd_subdirectory(src)\n')
    dc._save_cmake_files(dep)
    assert os.path.exists(f'{dep.src_dir}/mama.cmake')
    assert os.path.exists(f'{dep.src_dir}/src/mama.cmake')


def test_a_subproject_resolves_project_source_dir_to_its_own_dir(tmp_path):
    # project() in the subdirectory rebinds PROJECT_SOURCE_DIR, and CMAKE_SOURCE_DIR stays the top
    dep = _subdir_dep(tmp_path, 'project(Sub)\ninclude(${PROJECT_SOURCE_DIR}/mama.cmake)\n')
    dc._save_cmake_files(dep)
    assert os.path.exists(f'{dep.src_dir}/src/mama.cmake')
    assert not os.path.exists(f'{dep.src_dir}/mama.cmake')


def test_an_include_above_project_keeps_the_parent_project_source_dir(tmp_path):
    # cmake evaluates the include before project() rebinds PROJECT_SOURCE_DIR to the subdirectory
    dep = _subdir_dep(tmp_path, 'include(${PROJECT_SOURCE_DIR}/mama.cmake)\nproject(Sub)\n')
    dc._save_cmake_files(dep)
    assert os.path.exists(f'{dep.src_dir}/mama.cmake')
    assert not os.path.exists(f'{dep.src_dir}/src/mama.cmake')


def test_a_subdirectory_with_no_project_keeps_the_top_project_source_dir(tmp_path):
    dep = _subdir_dep(tmp_path, 'include(${PROJECT_SOURCE_DIR}/mama.cmake)\n')
    dc._save_cmake_files(dep)
    assert os.path.exists(f'{dep.src_dir}/mama.cmake')


def test_a_subdirectory_that_adds_itself_ends_the_scan(tmp_path):
    assert dc._proxy_paths(_subdir_dep(tmp_path, 'add_subdirectory(../src)\n')) == []


def test_a_subdirectory_named_by_a_cmake_variable_is_followed(tmp_path):
    dep = _subdir_dep(tmp_path, 'include(mama.cmake)\n', 'add_subdirectory(${CMAKE_CURRENT_SOURCE_DIR}/src)\n')
    dc._save_cmake_files(dep)
    assert os.path.exists(f'{dep.src_dir}/src/mama.cmake')


def test_a_shared_dir_added_by_two_subprojects_resolves_in_each_scope(tmp_path):
    # cmake reads one source dir once per project scope, and gives each a different PROJECT_SOURCE_DIR
    dep = _includes_proxy(_dep(tmp_path, 'leaf'), 'add_subdirectory(a)\nadd_subdirectory(b)\n')
    write_files(dep.src_dir, {'a/CMakeLists.txt': 'project(A)\nadd_subdirectory(${CMAKE_SOURCE_DIR}/shared sa)\n',
                              'b/CMakeLists.txt': 'project(B)\nadd_subdirectory(${CMAKE_SOURCE_DIR}/shared sb)\n',
                              'shared/CMakeLists.txt': 'include(${PROJECT_SOURCE_DIR}/mama.cmake)\n'})
    dc._save_cmake_files(dep)
    assert os.path.exists(f'{dep.src_dir}/a/mama.cmake')
    assert os.path.exists(f'{dep.src_dir}/b/mama.cmake')


def test_a_dollar_in_the_checkout_path_is_not_a_cmake_variable(tmp_path):
    # the expanded path holds a literal `$`, which must not read as a variable mama cannot expand
    dep = _includes_proxy(_dep(tmp_path, 'le$af'), 'include(${CMAKE_SOURCE_DIR}/sub/mama.cmake)\n')
    write_files(dep.src_dir, {'sub/keep.txt': ''})
    dc._save_cmake_files(dep)
    assert os.path.exists(f'{dep.src_dir}/sub/mama.cmake')
    assert not os.path.exists(f'{dep.src_dir}/mama.cmake')


def test_an_unquoted_argument_ends_at_a_line_comment(tmp_path):
    dep = _includes_proxy(_dep(tmp_path, 'leaf'), 'add_subdirectory(src#names the binary dir\n)\n')
    write_files(dep.src_dir, {'src/CMakeLists.txt': 'include(mama.cmake)\n'})
    dc._save_cmake_files(dep)
    assert os.path.exists(f'{dep.src_dir}/src/mama.cmake')


@pytest.mark.linux_host
def test_two_symlink_aliases_of_one_dir_each_get_a_proxy(tmp_path):
    # cmake reads each alias as its own source dir, so each one resolves CMAKE_CURRENT_SOURCE_DIR itself
    dep = _includes_proxy(_dep(tmp_path, 'leaf'), 'add_subdirectory(x/a)\nadd_subdirectory(y/b)\n')
    # `..` leaves the shared dir, so each alias names a proxy of its own rather than the one file both hold
    write_files(dep.src_dir, {'shared/CMakeLists.txt': 'include(${CMAKE_CURRENT_SOURCE_DIR}/../mama.cmake)\n'})
    for alias in ('x', 'y'):
        os.makedirs(f'{dep.src_dir}/{alias}', exist_ok=True)
    os.symlink(f'{dep.src_dir}/shared', f'{dep.src_dir}/x/a')
    os.symlink(f'{dep.src_dir}/shared', f'{dep.src_dir}/y/b')
    dc._save_cmake_files(dep)
    assert os.path.exists(f'{dep.src_dir}/x/mama.cmake') and os.path.exists(f'{dep.src_dir}/y/mama.cmake')


@pytest.mark.linux_host
def test_a_symlink_loop_stops_the_walk(tmp_path):
    # a dir that symlinks to its own parent gives every level a new name, so only the depth cap ends it
    dep = _includes_proxy(_dep(tmp_path, 'leaf'), 'add_subdirectory(sub)\n')
    write_files(dep.src_dir, {'sub/CMakeLists.txt': 'add_subdirectory(loop)\n'})
    os.symlink(f'{dep.src_dir}/sub', f'{dep.src_dir}/sub/loop')
    dc._save_cmake_files(dep)   # it returns, rather than walking the alias chain forever


@pytest.mark.parametrize('arg, unknown', [
    ('src$dir',       False),   # a bare `$` is ordinary content of an argument
    ('${FOO}/x',      True),
    ('$ENV{HOME}/x',  True),
    ('$CACHE{C}/x',   True),
], ids=['bare', 'brace', 'env', 'cache'])
def test_only_a_reference_form_names_an_unknown_variable(arg, unknown):
    assert has_unknown_cmake_var(arg) is unknown


def test_a_bare_dollar_names_the_dir_it_spells(tmp_path):
    # cmake names a variable with `${}`, `$ENV{}` or `$CACHE{}`, so this `$` stops no branch
    dep = _includes_proxy(_dep(tmp_path, 'leaf'), 'add_subdirectory(src$dir)\n')
    write_files(dep.src_dir, {'src$dir/CMakeLists.txt': 'include(mama.cmake)\n'})
    dc._save_cmake_files(dep)
    assert os.path.exists(f'{dep.src_dir}/src$dir/mama.cmake')


@pytest.mark.linux_host
def test_a_tree_deeper_than_any_cap_still_reaches_its_leaf(tmp_path):
    # cmake reads 65 nested levels, so only a real cycle may stop the walk, never a depth count
    dep = _includes_proxy(_dep(tmp_path, 'leaf'), 'add_subdirectory(d)\n')
    rel = ''
    for level in range(65):
        rel = os.path.join(rel, 'd')
        leaf = level == 64
        write_files(dep.src_dir, {f'{rel}/CMakeLists.txt':
                                  'include(mama.cmake)\n' if leaf else 'add_subdirectory(d)\n'})
    dc._save_cmake_files(dep)
    assert os.path.exists(f'{dep.src_dir}/{rel}/mama.cmake')


def test_a_bracket_comment_before_a_subdirectory_stops_that_branch(tmp_path):
    # `#` ends an unquoted argument, so `#[[note]] src` names no dir and mama reads no deeper
    dep = _subdir_dep(tmp_path, 'include(mama.cmake)\n', 'add_subdirectory(#[[note]] src)\n')
    dc._save_cmake_files(dep)
    assert not os.path.exists(f'{dep.src_dir}/src/mama.cmake')

