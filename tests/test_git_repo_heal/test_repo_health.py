"""Pins repo_health_from_disk against real git: it must never disagree, only defer."""
import os, shutil, subprocess
from unittest.mock import patch
import pytest

from mama.types.git import repo_health_from_disk
from mama.utils.fileio import remove_tree
from testutils import make_mock_dep


def _git(args, cwd):
    return subprocess.run(['git', *args], cwd=str(cwd), capture_output=True, text=True)


def _repo(path, commit=True, init_args=()):
    path.mkdir(parents=True, exist_ok=True)
    if _git(['init', '-q', *init_args], path).returncode != 0: return None
    _git(['config', 'user.email', 't@t'], path); _git(['config', 'user.name', 't'], path)
    if commit:
        (path / 'f.txt').write_text('hi\n')
        _git(['add', '-A'], path); _git(['commit', '-q', '-m', 'init'], path)
    return path


def _git_says_own_repo(path) -> bool:
    """What mama's subprocess check concludes, so the test tracks git rather than a copy of its rules."""
    out = _git(['rev-parse', '--show-toplevel', '--verify', '-q', 'HEAD'], path).stdout.splitlines()
    return len(out) >= 2 and os.path.realpath(out[0]) == os.path.realpath(str(path))


def _break(git_dir, how):
    if how == 'no HEAD':          os.remove(git_dir / 'HEAD')
    elif how == 'empty HEAD':     (git_dir / 'HEAD').write_text('')
    elif how == 'no objects':     remove_tree(str(git_dir / 'objects'))
    elif how == 'no refs':        remove_tree(str(git_dir / 'refs'))
    elif how == 'empty .git':     (remove_tree(str(git_dir)), git_dir.mkdir())
    elif how == 'no .git':        remove_tree(str(git_dir))


@pytest.mark.parametrize('how', ['no HEAD', 'empty HEAD', 'no objects', 'no refs', 'empty .git', 'no .git'])
def test_a_shape_that_sends_git_upward_reads_as_broken(tmp_path, how):
    # THE case the check must never miss: git resolves a repo ABOVE this dir, so a reset lands there
    parent = _repo(tmp_path / 'parent')
    child = _repo(parent / 'dep')
    _break(child / '.git', how)
    assert not _git_says_own_repo(child)
    assert repo_health_from_disk(str(child)) is False


@pytest.mark.parametrize('shape', ['plain', 'shallow clone', 'tag clone', 'detached'])
def test_every_real_dependency_shape_reads_as_healthy(tmp_path, shape):
    origin = _repo(tmp_path / 'origin')
    _git(['tag', 'v1.0.0'], origin)
    url = 'file:///' + str(origin).replace(os.sep, '/').lstrip('/')
    dst = tmp_path / 'dep'
    if shape == 'plain':
        _repo(dst)
    else:
        args = ['--depth', '1'] + (['--branch', 'v1.0.0'] if shape == 'tag clone' else [])
        assert _git(['clone', '-q', *args, url, str(dst)], tmp_path).returncode == 0
        if shape == 'detached':
            _git(['checkout', '-q', '--detach', 'HEAD'], dst)
    assert _git_says_own_repo(dst)
    assert repo_health_from_disk(str(dst)) is True


@pytest.mark.parametrize('shape', ['unborn HEAD', 'missing ref', 'gitdir file'])
def test_a_shape_only_git_can_settle_defers(tmp_path, shape):
    d = tmp_path / 'dep'
    if shape == 'unborn HEAD':
        _repo(d, commit=False)
    elif shape == 'missing ref':
        _repo(d); (d / '.git' / 'HEAD').write_text('ref: refs/heads/gone\n')
    else:
        _repo(d)
        real = tmp_path / 'real_gitdir'
        shutil.move(str(d / '.git'), str(real))
        (d / '.git').write_text(f'gitdir: {real}\n')
    assert repo_health_from_disk(str(d)) is None


def test_a_reftable_repo_defers_instead_of_reading_as_broken(tmp_path):
    # a healthy reftable repo has an empty refs/heads and HEAD -> refs/heads/.invalid. A check that
    # concluded from the missing ref file would call it broken, and mama would refuse to update it
    d = tmp_path / 'dep'
    if _repo(d, init_args=['--ref-format=reftable']) is None:
        pytest.skip('this git has no reftable backend')
    assert _git_says_own_repo(d)
    assert repo_health_from_disk(str(d)) is None


def test_a_healthy_clone_spawns_no_git_process(tmp_path):
    dep = make_mock_dep(tmp_path, name='dep')
    _repo(tmp_path / 'dep')
    dep.src_dir = str(tmp_path / 'dep')
    with patch('mama.types.git.execute_piped') as piped:
        assert dep.dep_source._is_repo_broken(dep) is False
    piped.assert_not_called()
