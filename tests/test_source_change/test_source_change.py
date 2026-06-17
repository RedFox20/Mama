"""Pins working-tree fingerprint detection: `mama build` must rebuild a git dep
whose source was edited in place, without a full status check or reconfigure."""
import os
from pathlib import Path
from unittest.mock import Mock, patch
from testutils import make_mock_dep
from mama.utils.sub_process import execute_piped


def _init_repo(src_dir):
    os.makedirs(src_dir, exist_ok=True)
    for cmd in ['init -q', 'config user.email t@t', 'config user.name t',
                'commit --allow-empty -q -m init']:
        execute_piped(['git', *cmd.split()], cwd=src_dir)


def _git_dep_with_repo(tmp_path):
    dep = make_mock_dep(tmp_path)
    _init_repo(dep.src_dir)
    (Path(dep.src_dir) / 'lib.cpp').write_text('int f(){return 1;}\n')
    execute_piped(['git', 'add', '-A'], cwd=dep.src_dir)
    execute_piped(['git', 'commit', '-q', '-m', 'src'], cwd=dep.src_dir)
    return dep


def test_clean_tree_fingerprint_is_empty(tmp_path):
    dep = _git_dep_with_repo(tmp_path)
    assert dep.dep_source.working_tree_fingerprint(dep) == ''


def test_modified_tracked_file_changes_fingerprint(tmp_path):
    dep = _git_dep_with_repo(tmp_path)
    git = dep.dep_source
    git.save_status(dep)  # snapshot clean state
    assert not git.source_tree_changed(dep)

    (Path(dep.src_dir) / 'lib.cpp').write_text('int f(){return 2;}\n')
    assert git.working_tree_fingerprint(dep) != ''
    assert git.source_tree_changed(dep)


def test_untracked_file_changes_fingerprint(tmp_path):
    dep = _git_dep_with_repo(tmp_path)
    git = dep.dep_source
    git.save_status(dep)
    (Path(dep.src_dir) / 'extra.h').write_text('#pragma once\n')
    assert git.source_tree_changed(dep)


def test_save_status_round_trips_fingerprint(tmp_path):
    dep = _git_dep_with_repo(tmp_path)
    git = dep.dep_source
    (Path(dep.src_dir) / 'lib.cpp').write_text('int f(){return 3;}\n')
    git.save_status(dep)
    assert not git.source_tree_changed(dep)  # stored snapshot matches current edit

    (Path(dep.src_dir) / 'lib.cpp').write_text('int f(){return 4;}\n')
    assert git.source_tree_changed(dep)  # further edit detected


def test_legacy_status_without_tree_line_treated_as_clean(tmp_path):
    dep = _git_dep_with_repo(tmp_path)
    git = dep.dep_source
    from mama.util import save_file_if_contents_changed
    save_file_if_contents_changed(git.git_status_file(dep), f"{git.url}\n\nmain\nabc1234\n")
    assert not git.source_tree_changed(dep)  # clean tree vs legacy 4-line status


def _should_build_reasons(dep, loaded_from_pkg):
    conf = dep.config; conf.print = True
    target = Mock(name='t'); target.name = dep.name; target.args = []; target.build_products = []
    dep.target = target  # so the fall-through (no source change) path doesn't crash
    with patch('mama.build_dependency.warning') as w:
        built = dep._should_build(conf, target, is_target=False, git_changed=False, loaded_from_pkg=loaded_from_pkg)
    return built, ' '.join(str(c) for c in w.call_args_list)


def test_source_edit_rebuilds_git_dep_even_when_from_artifactory(tmp_path):
    # regression: a prebuilt pkg can also have a source clone on disk; a `not from_artifactory`
    # guard wrongly skipped the working-tree check for exactly that dep.
    dep = _git_dep_with_repo(tmp_path)
    dep.from_artifactory = True
    (Path(dep.src_dir) / 'lib.cpp').write_text('int f(){return 9;}\n')
    built, reasons = _should_build_reasons(dep, loaded_from_pkg=True)
    assert built and 'source modified' in reasons
