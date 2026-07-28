"""Pins broken-.git handling: an effectively empty dir is wiped and re-cloned, real source never is."""
import contextlib, os
from unittest.mock import patch
from testutils import make_mock_dep
from mama.util import has_source_content, is_dir_empty


def _seed(dep, *names):
    """Materialise src_dir with the given entries; '.git' is created as a directory."""
    os.makedirs(dep.src_dir, exist_ok=True)
    for n in names:
        if n == '.git': os.makedirs(f'{dep.src_dir}/.git', exist_ok=True)
        else: open(f'{dep.src_dir}/{n}', 'w').close()


@contextlib.contextmanager
def _stubbed(dep, broken):
    """dependency_checkout with the git side stubbed; yields the (wipe, clone) mocks."""
    git = dep.dep_source
    with patch.object(git, '_is_repo_broken', return_value=broken), patch.object(git, '_sync_remote_url'), \
         patch.object(git, 'reclone_wipe') as wipe, patch.object(git, 'clone_or_pull') as clone:
        yield wipe, clone


def _checkout(dep, broken=False):
    """(result, wiped, cloned) for the checkout."""
    with _stubbed(dep, broken) as (wipe, clone):
        result = dep.dep_source.dependency_checkout(dep)
    return result, wipe.called, clone.called


def test_is_repo_broken_true_when_head_unresolvable(tmp_path):
    dep = make_mock_dep(tmp_path)
    with patch('mama.types.git.execute_piped', return_value=''):  # rev-parse -q printed nothing -> broken
        assert dep.dep_source._is_repo_broken(dep)


def test_is_repo_broken_false_for_healthy_head(tmp_path):
    dep = make_mock_dep(tmp_path)
    with patch('mama.types.git.execute_piped', return_value='a1b2c3d'):
        assert not dep.dep_source._is_repo_broken(dep)


def test_has_source_content_ignores_what_mama_generated(tmp_path):
    d = tmp_path / 'dep'
    assert not has_source_content(str(d))             # doesn't exist
    d.mkdir()
    assert not has_source_content(str(d))
    (d / 'mama.cmake').write_text('')
    assert not has_source_content(str(d))             # the exact shape of the reported broken ffmpeg dir
    (d / '.git').mkdir()
    assert not has_source_content(str(d))             # a half-finished clone has no working tree to lose
    (d / 'ffmpeg.c').write_text('')
    assert has_source_content(str(d))


def test_has_source_content_sees_source_hidden_in_subdirs(tmp_path):
    # is_dir_empty only counts top-level files, so an rsync'd tree with no root-level file reads as
    # 'empty' to it; the wipe guard must not inherit that blind spot
    d = tmp_path / 'dep'
    (d / 'libavcodec').mkdir(parents=True)
    assert is_dir_empty(str(d)) and has_source_content(str(d))


def test_a_dir_holding_only_mama_generated_files_heals(tmp_path):
    # the reported case: ffmpeg/ held nothing but the mama.cmake proxy mama wrote there itself
    dep = make_mock_dep(tmp_path)
    _seed(dep, 'mama.cmake')
    assert _checkout(dep) == (True, True, True)


def test_a_broken_git_with_nothing_checked_out_heals(tmp_path):
    dep = make_mock_dep(tmp_path)
    _seed(dep, '.git')
    assert _checkout(dep, broken=True) == (True, True, True)


def test_an_rsynced_sandbox_copy_without_git_is_never_clobbered(tmp_path):
    # sandbox builds rsync a dep's source (often excluding .git) and may edit it; re-cloning over
    # that destroys the sandbox, which is the whole point of modular local development
    dep = make_mock_dep(tmp_path)
    _seed(dep, 'mama.cmake', 'ffmpeg.c')
    assert _checkout(dep) == (False, False, False)


def test_a_sandbox_copy_with_a_broken_git_is_never_clobbered(tmp_path):
    dep = make_mock_dep(tmp_path)
    _seed(dep, '.git', 'ffmpeg.c')
    assert _checkout(dep, broken=True) == (False, False, False)


def test_wiping_the_target_still_forces_the_reclone(tmp_path):
    # the guard must not lock the user out of an intentional `mama wipe <target>`
    dep = make_mock_dep(tmp_path, reclone=True)
    dep.config.target_matches.return_value = True
    _seed(dep, 'ffmpeg.c')
    assert _checkout(dep) == (True, True, True)


def test_wiping_a_different_target_does_not_clobber_this_one(tmp_path):
    dep = make_mock_dep(tmp_path, reclone=True)  # `mama wipe other` must not take out a sandboxed dep
    _seed(dep, 'ffmpeg.c')
    assert _checkout(dep) == (False, False, False)


def test_checkout_leaves_a_healthy_clone_alone(tmp_path):
    dep = make_mock_dep(tmp_path)
    _seed(dep, '.git', 'ffmpeg.c')  # non-target dep of a plain build: no changes -> no pull, no wipe
    assert _checkout(dep) == (False, False, False)


def _wipe_call(dep, broken=False):
    """The reclone_wipe(...) call dependency_checkout makes; None if it never wiped."""
    with _stubbed(dep, broken) as (wipe, _):
        dep.dep_source.dependency_checkout(dep)
    return wipe.call_args


def test_an_automatic_heal_wipes_only_the_source(tmp_path):
    # dep_dir is shared by every platform: a tree broken for THIS one must not delete a sibling's
    # artifactory package/shim/build output - a concurrent `mama <host> build` may be reading it
    dep = make_mock_dep(tmp_path)
    _seed(dep, '.git')
    assert _wipe_call(dep, broken=True).kwargs == {'source_only': True}


def test_an_explicit_mama_wipe_still_drops_the_whole_dep_dir(tmp_path):
    dep = make_mock_dep(tmp_path, reclone=True)
    dep.config.target_matches.return_value = True
    _seed(dep, 'ffmpeg.c')  # source but no usable .git -> the guard defers to the explicit wipe
    assert _wipe_call(dep).kwargs == {'source_only': False}


def test_reclone_wipe_source_only_keeps_sibling_platforms_and_the_cached_zip(tmp_path):
    dep = make_mock_dep(tmp_path)
    _seed(dep, '.git', 'ffmpeg.c')
    sibling = os.path.join(dep.dep_dir, 'android'); os.makedirs(sibling, exist_ok=True)
    open(os.path.join(sibling, 'mama_shim'), 'w').close()
    zip_path = os.path.join(dep.dep_dir, 'libfoo-android-29-clang21.0-arm64-release-abc1234.zip')
    open(zip_path, 'w').close()

    dep.dep_source.reclone_wipe(dep, source_only=True)
    assert not os.path.exists(dep.src_dir)                                  # the broken tree is gone
    assert os.path.exists(os.path.join(sibling, 'mama_shim'))               # android's shim survives
    assert os.path.exists(zip_path)                                         # so does the cached package

    dep.dep_source.reclone_wipe(dep)  # explicit wipe: everything goes
    assert not os.path.exists(dep.dep_dir)
