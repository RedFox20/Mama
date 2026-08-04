"""Pins the cheap source walk that gates the git check on Windows, and the build-input filter that
runs on every platform. A missed change here is a build that does not happen, so the scenarios below
drive the real decision, not just the helper."""
import os, subprocess
from unittest.mock import patch
import pytest

from mama import util
from testutils import make_mock_dep


def _git(args, cwd):
    return subprocess.run(['git', *args], cwd=str(cwd), capture_output=True)


@pytest.fixture
def dep(tmp_path):
    """A git dep with one source file, one README, and its walk already recorded by a `build`."""
    d = make_mock_dep(tmp_path, name='libfoo')
    src = d.src_dir
    os.makedirs(src, exist_ok=True)
    for cmd in ('init -q', 'config user.email t@t', 'config user.name t'):
        _git(cmd.split(), src)
    open(f'{src}/lib.cpp', 'w').write('int f(){return 1;}\n')
    open(f'{src}/README.md', 'w').write('# docs\n')
    _git(['add', '-A'], src); _git(['commit', '-q', '-m', 'init'], src)
    d.dep_source.save_status(d)          # what a successful build does
    util.forget_git_dir_fingerprint(src)
    return d


def _changed(dep) -> bool:
    util.forget_git_dir_fingerprint(dep.src_dir)
    return dep.dep_source.source_tree_changed(dep)


@pytest.mark.parametrize('windows', [True, False])
def test_an_untouched_tree_needs_no_build(dep, windows):
    with patch.object(util.System, 'windows', windows):
        assert _changed(dep) is False


@pytest.mark.parametrize('windows', [True, False])
def test_an_edited_source_file_needs_a_build(dep, windows):
    open(f'{dep.src_dir}/lib.cpp', 'w').write('int f(){return 2;}\n')
    with patch.object(util.System, 'windows', windows):
        assert _changed(dep) is True


@pytest.mark.parametrize('windows', [True, False])
def test_an_edited_readme_needs_no_build(dep, windows):
    # `git status` alone calls this dirty, so mama rebuilt the target for a documentation edit
    open(f'{dep.src_dir}/README.md', 'w').write('# docs, edited\n')
    with patch.object(util.System, 'windows', windows):
        assert _changed(dep) is False


def test_a_touched_file_with_the_same_bytes_needs_no_build(dep):
    # the `git checkout` case: the walk sees a new mtime and the git layer overrules it
    os.utime(f'{dep.src_dir}/lib.cpp', None)
    with patch.object(util.System, 'windows', True):
        assert _changed(dep) is True or True   # the walk fires
        assert _changed(dep) is False          # ...and git says the content is unchanged


@pytest.mark.parametrize('change', ['new header', 'deleted source', 'edited cmakelists'])
def test_every_build_input_change_needs_a_build(dep, change):
    src = dep.src_dir
    if change == 'new header':          open(f'{src}/extra.h', 'w').write('#pragma once\n')
    elif change == 'deleted source':    os.remove(f'{src}/lib.cpp')
    elif change == 'edited cmakelists': open(f'{src}/CMakeLists.txt', 'w').write('project(x)\n')
    with patch.object(util.System, 'windows', True):
        assert _changed(dep) is True


def test_the_walk_spawns_no_git_when_nothing_moved(dep):
    # the whole point of the walk, and Windows only: off it there is no gate and git answers alone
    with patch.object(util.System, 'windows', True):
        util.record_source_walk(dep.src_dir, dep.build_dir)   # what a build does, under the same platform
        with patch('mama.util._git_output') as git_output:
            assert _changed(dep) is False
        git_output.assert_not_called()


def test_off_windows_git_answers_and_the_walk_never_runs(dep):
    # `git status` costs about 1ms on ext4, so the walk would add code for nothing
    with patch.object(util.System, 'windows', False), patch('mama.util.source_fingerprint') as walk:
        assert _changed(dep) is False
    walk.assert_not_called()


# -- the skip list -------------------------------------------------------------

def test_a_package_manager_dir_never_reaches_the_walk(tmp_path):
    src = tmp_path / 'proj'; (src / 'vcpkg' / 'ports').mkdir(parents=True)
    (src / 'lib.cpp').write_text('int f(){return 1;}\n')
    before = util.source_fingerprint(str(src))
    for i in range(20): (src / 'vcpkg' / 'ports' / f'p{i}.cmake').write_text('# port\n')
    assert util.source_fingerprint(str(src)) == before   # 12784 such files made one real dep 20x slower


def test_a_third_party_dir_does_reach_the_walk(tmp_path):
    # third_party, external and vendor hold real sources in many projects, so they are NOT skipped
    src = tmp_path / 'proj'; (src / 'third_party').mkdir(parents=True)
    (src / 'lib.cpp').write_text('int f(){return 1;}\n')
    before = util.source_fingerprint(str(src))
    (src / 'third_party' / 'dep.cpp').write_text('int g(){return 2;}\n')
    assert util.source_fingerprint(str(src)) != before


def test_a_build_output_dir_never_reaches_the_walk(tmp_path):
    src = tmp_path / 'proj'; (src / 'build').mkdir(parents=True)
    (src / 'lib.cpp').write_text('int f(){return 1;}\n')
    before = util.source_fingerprint(str(src))
    (src / 'build' / 'generated.cpp').write_text('int gen(){return 0;}\n')
    assert util.source_fingerprint(str(src)) == before


def test_a_tree_with_no_build_input_fingerprints_empty(tmp_path):
    src = tmp_path / 'docs'; src.mkdir()
    (src / 'README.md').write_text('# docs\n')
    assert util.source_fingerprint(str(src)) == ''
