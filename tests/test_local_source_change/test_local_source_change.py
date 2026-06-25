"""Pins local-dep modification detection: a local package tracked by an enclosing git repo
must trigger a cmake build when its own subfolder has uncommitted edits, and stay fast otherwise."""
from pathlib import Path
from unittest.mock import Mock, patch
from testutils import make_mock_local_dep
from mama.utils.sub_process import execute_piped


def _root_repo_with_local_pkg(tmp_path):
    root = tmp_path / 'root'
    sub = root / 'libs' / 'foo'
    sub.mkdir(parents=True)
    (sub / 'lib.cpp').write_text('int f(){return 1;}\n')
    for cmd in ['init -q', 'config user.email t@t', 'config user.name t', 'add -A', 'commit -q -m init']:
        execute_piped(['git', *cmd.split()], cwd=str(root))
    return make_mock_local_dep(tmp_path, src_dir=sub)


def test_modified_tracked_file_changes_fingerprint(tmp_path):
    dep = _root_repo_with_local_pkg(tmp_path); src = dep.dep_source
    src.save_status(dep)
    assert not src.source_tree_changed(dep)  # clean subfolder after snapshot
    (Path(dep.src_dir) / 'lib.cpp').write_text('int f(){return 2;}\n')
    assert src.source_tree_changed(dep)


def test_untracked_file_changes_fingerprint(tmp_path):
    dep = _root_repo_with_local_pkg(tmp_path); src = dep.dep_source
    src.save_status(dep)
    (Path(dep.src_dir) / 'extra.h').write_text('#pragma once\n')
    assert src.source_tree_changed(dep)


def test_parent_change_outside_subfolder_is_ignored(tmp_path):
    dep = _root_repo_with_local_pkg(tmp_path); src = dep.dep_source
    src.save_status(dep)
    (Path(dep.src_dir).parent.parent / 'README.md').write_text('hello\n')  # change elsewhere in the root repo
    assert not src.source_tree_changed(dep)


def test_non_git_local_dir_is_treated_as_clean(tmp_path):
    sub = tmp_path / 'plain' / 'foo'; sub.mkdir(parents=True)
    (sub / 'lib.cpp').write_text('x\n')
    dep = make_mock_local_dep(tmp_path, src_dir=sub)
    assert dep.dep_source.working_tree_fingerprint(dep) == ''
    assert not dep.dep_source.source_tree_changed(dep)


def _should_build_reasons(dep):
    conf = dep.config; conf.print = True
    target = Mock(name='t'); target.name = dep.name; target.args = []; target.build_products = ['x']
    dep.target = target
    with patch.object(dep, 'find_first_missing_build_product', return_value=None), \
         patch.object(dep, 'find_missing_dependency', return_value=None), \
         patch.object(dep, 'update_mamafile_tag', return_value=False), \
         patch.object(dep, 'update_cmakelists_tag', return_value=False), \
         patch('mama.build_dependency.warning') as w:
        built = dep._should_build(conf, target, is_target=False, git_changed=False, loaded_from_pkg=True)
    return built, ' '.join(str(c) for c in w.call_args_list)


def test_clean_subfolder_does_not_build(tmp_path):
    dep = _root_repo_with_local_pkg(tmp_path)
    dep.dep_source.save_status(dep)
    built, reasons = _should_build_reasons(dep)
    assert not built and 'source modified' not in reasons


def test_modified_subfolder_triggers_build(tmp_path):
    dep = _root_repo_with_local_pkg(tmp_path)
    dep.dep_source.save_status(dep)
    (Path(dep.src_dir) / 'lib.cpp').write_text('int f(){return 5;}\n')
    built, reasons = _should_build_reasons(dep)
    assert built and 'source modified' in reasons
