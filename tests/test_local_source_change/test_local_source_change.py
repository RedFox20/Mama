"""Pins local-dep modification detection: a local package tracked by an enclosing git repo
must trigger a cmake build when its own subfolder has uncommitted edits, and stay fast otherwise."""
from pathlib import Path
from testutils import make_git_root_with_local_pkgs, make_mock_local_dep, should_build_reasons


def _root_repo_with_local_pkg(tmp_path):
    return make_git_root_with_local_pkgs(tmp_path)[0]


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
    """Only the source check may answer here, so the reasons below it are silenced."""
    return should_build_reasons(dep, build_products=['x'], isolate=True)


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
