"""Pins unpublish: what each selector names, what the prompt guards, and the local purge that follows."""
import os
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from testutils import make_package_target, make_project_dir, stub_loaders, stub_runners

from mama.main import mamabuild, set_target_from_unused_args

import mama.artifactory_unpublish as up
from mama.build_config import BuildConfig
from mama.utils.paths import MAMA_SHIM_FILENAME


NAME = 'libfoo'


def _archive(version, compiler='gcc13.3', day='07', platform='linux'):
    return up.Archive(f'{NAME}-{platform}-ubuntu24-{compiler}-x64-release-{version}.zip',
                      f'202608{day}120000', 1024 * 1024)


# --- naming: the one thing that must never over-match -------------------------

@pytest.mark.parametrize('filename, expected', [
    (f'{NAME}-linux-ubuntu24-gcc13.3-x64-release-caf5158.zip', 'caf5158'),
    (f'{NAME}-windows-10-msvc14.51-x64-release-main-a491bd2.zip', 'main-a491bd2'),
    (f'{NAME}-linux-ubuntu24-gcc14.3-x64-release-asan-caf5158.zip', 'asan-caf5158'),
    ('other-linux-ubuntu24-gcc13.3-x64-release-caf5158.zip', ''),   # a neighbour is never ours
    (f'{NAME}-linux-ubuntu24-gcc13.3-x64.zip', ''),                 # too few fields to name a version
    ('README.txt', ''),
])
def test_the_version_a_name_carries(filename, expected):
    assert up.archive_version(NAME, filename) == expected


@pytest.mark.parametrize('name', [
    f'../../{NAME}-linux-ubuntu24-gcc13.3-x64-release-v1.zip',
    f'/tmp/{NAME}-linux-ubuntu24-gcc13.3-x64-release-v1.zip',
    f'..\\{NAME}-linux-ubuntu24-gcc13.3-x64-release-v1.zip',
    f'{NAME}-linux-ubuntu24-gcc13.3-x64-release-v1.zip/../../x',
])
def test_a_server_name_that_is_a_path_is_never_selected(name):
    # the local purge deletes by this name, so a hostile or broken server must not reach outside dep_dir
    assert up.archive_version(NAME, name) == ''
    assert up.select(NAME, [up.Archive(name, '20260807120000', 1)], 'prune-all') == []


def test_the_local_purge_only_ever_touches_names_inside_the_dep_dir(tmp_path):
    outside = tmp_path / 'victim.zip'
    outside.write_text('precious')
    target = _target_with_cache(tmp_path, [])
    escape = up.Archive(f'../../{outside.name}', '20260807120000', 1)
    assert up.purge_local(target, [escape]) == 0
    assert outside.exists()


def test_a_version_selector_takes_every_platform_and_no_neighbour():
    ours = [_archive('caf5158'), _archive('caf5158', compiler='msvc14.51', platform='windows')]
    others = [_archive('deadbee'), up.Archive('other-linux-ubuntu24-gcc13.3-x64-release-caf5158.zip', '1', 1)]
    picked = up.select(NAME, ours + others, 'caf5158')
    assert sorted(a.filename for a in picked) == sorted(a.filename for a in ours)


def test_prune_all_takes_every_version_but_leaves_a_foreign_file():
    stray = up.Archive('notes.txt', '20260101000000', 10)
    picked = up.select(NAME, [_archive('a1'), _archive('b2'), stray], 'prune-all')
    assert sorted(a.filename for a in picked) == sorted([_archive('a1').filename, _archive('b2').filename])


# --- prune-old counts versions, never archives --------------------------------

def _many_versions(count, per_version=3):
    """`count` versions, each published on its own day, each with `per_version` compiler variants."""
    return [_archive(f'v{i:02}', compiler=f'gcc{c}', day=f'{i + 1:02}')
            for i in range(count) for c in range(per_version)]


def test_prune_old_keeps_whole_versions_not_archive_counts():
    archives = _many_versions(25, per_version=3)  # 75 archives, 25 versions
    doomed = up.select(NAME, archives, 'prune-old', keep=20)
    survivors = set(archives) - set(doomed)
    assert len(up.group_by_version(NAME, list(survivors))) == 20
    assert len(doomed) == 5 * 3  # the 5 oldest versions, every variant of each


def test_prune_old_keeps_everything_when_there_is_less_than_the_limit():
    assert up.select(NAME, _many_versions(4), 'prune-old', keep=20) == []


def test_a_version_is_as_fresh_as_its_freshest_archive():
    # a rebuild of an old commit keeps that version alive, which is the point of taking the max
    old = _archive('old', day='01')
    rebuilt = _archive('old', compiler='gcc14.3', day='28')
    newer = _archive('new', day='10')
    assert up.newest_first(up.group_by_version(NAME, [old, rebuilt, newer])) == ['old', 'new']


# --- the listing a human reads before confirming -------------------------------

class _Named:
    """A hashable stand-in for a target: describe_run keys its dict by target and reads its dep."""
    def __init__(self, name):
        self.name = name
        self.dep = SimpleNamespace(dep_dir='/nowhere', build_dir='/nowhere/linux', src_dir='/nowhere/src',
                                   is_artifactory_shim=lambda: False)


def test_the_listing_carries_a_date_and_a_size():
    target = _Named(NAME)
    text = up.describe_run({target: [_archive('caf5158')]}, 'files.example.com')
    assert '2026-Aug-07' in text and '1.0MB' in text
    assert '1 archive(s), 1 version(s), 1 target(s) on files.example.com' in text


def test_the_listing_covers_every_target_of_the_run():
    a, b = _Named(NAME), _Named('other')
    other = up.Archive('other-linux-ubuntu24-gcc13.3-x64-release-zz.zip', '20260801120000', 512)
    text = up.describe_run({a: [_archive('caf5158')], b: [other]}, 'files.example.com')
    assert '2 archive(s), 2 version(s), 2 target(s)' in text
    assert text.index(other.filename) < text.index(_archive('caf5158').filename)  # oldest first


def test_an_unreadable_date_reads_as_a_question_mark():
    assert up.Archive('x.zip', '', 0).date() == '?'
    assert up.Archive('x.zip', '20261399000000', 0).date() == '?'  # month 13 is not a month


# --- the prompt ---------------------------------------------------------------

@pytest.mark.parametrize('answer, deleted', [('y', True), ('yes', True), ('n', False), ('', False)])
def test_the_prompt_decides_whether_anything_goes(answer, deleted):
    with patch('mama.artifactory_unpublish.is_headless', return_value=False), \
         patch('builtins.input', return_value=answer):
        assert up._confirm('listing', assume_yes=False) is deleted


def test_a_headless_run_refuses_instead_of_hanging():
    # a CI job has no stdin, and a blocked input() would hang the job until it times out
    with patch('mama.artifactory_unpublish.is_headless', return_value=True), \
         patch('builtins.input', side_effect=AssertionError('must not prompt')):
        assert up._confirm('listing', assume_yes=False) is False


def test_yes_on_the_command_line_skips_the_prompt():
    with patch('builtins.input', side_effect=AssertionError('must not prompt')):
        assert up._confirm('listing', assume_yes=True) is True


# --- the local purge ----------------------------------------------------------

def _target_with_cache(tmp_path, archives, shim=''):
    """A target holding a cached zip per archive. `shim` names the archive its shim marker serves."""
    target = make_package_target(tmp_path, package=None)
    dep = target.dep
    os.makedirs(dep.dep_dir, exist_ok=True)
    os.makedirs(dep.build_dir, exist_ok=True)
    for a in archives: open(os.path.join(dep.dep_dir, a.filename), 'w').write('zip')
    if shim:
        open(os.path.join(dep.build_dir, MAMA_SHIM_FILENAME), 'w').write(f'shim 1\narchive {shim}\n')
    return target


def test_the_purge_removes_the_cached_zip_of_every_deleted_archive(tmp_path):
    archives = [_archive('caf5158'), _archive('caf5158', compiler='msvc14.51')]
    target = _target_with_cache(tmp_path, archives)
    assert up.purge_local(target, archives) == 2
    assert not os.path.exists(os.path.join(target.dep.dep_dir, archives[0].filename))


def test_the_purge_drops_a_shim_that_serves_a_deleted_archive(tmp_path):
    # the server copy is gone, so a shim that keeps serving it would outlive the package itself
    archives = [_archive('caf5158')]
    target = _target_with_cache(tmp_path, archives, shim=archives[0].filename[:-4])
    up.purge_local(target, archives)
    assert not os.path.exists(target.dep.build_dir)


def test_the_purge_keeps_a_shim_that_serves_a_version_this_run_kept(tmp_path):
    # that package is still on the server, so dropping the shim would force a needless re-fetch
    archives = [_archive('caf5158')]
    target = _target_with_cache(tmp_path, archives, shim=_archive('kept').filename[:-4])
    up.purge_local(target, archives)
    assert os.path.exists(target.dep.build_dir)


def _platform_shim(target, platform: str, archive_stem: str):
    """A build dir for another platform, carrying a shim marker that names `archive_stem`."""
    build_dir = os.path.join(target.dep.dep_dir, platform)
    os.makedirs(build_dir, exist_ok=True)
    open(os.path.join(build_dir, MAMA_SHIM_FILENAME), 'w').write(f'shim 1\narchive {archive_stem}\n')
    return build_dir


def test_the_purge_reaches_the_shim_of_every_platform(tmp_path):
    # the run builds linux, and android holds a shim for the same archive. Leaving it there means the
    # machine keeps naming a package the server no longer has.
    deleted = _archive('caf5158')
    target = _target_with_cache(tmp_path, [deleted])
    android = _platform_shim(target, 'android', deleted.filename[:-4])
    up.purge_local(target, [deleted])
    assert not os.path.exists(android)


def test_a_shim_of_another_platform_survives_when_its_archive_stays(tmp_path):
    deleted = _archive('caf5158')
    target = _target_with_cache(tmp_path, [deleted])
    android = _platform_shim(target, 'android', _archive('kept').filename[:-4])
    up.purge_local(target, [deleted])
    assert os.path.exists(android)


def test_the_purge_never_removes_a_dir_that_holds_a_clone(tmp_path):
    # a `.git` inside means a working tree, whatever a stale marker beside it claims
    deleted = _archive('caf5158')
    target = _target_with_cache(tmp_path, [deleted])
    cloned = _platform_shim(target, 'windows', deleted.filename[:-4])
    os.makedirs(os.path.join(cloned, '.git'))
    up.purge_local(target, [deleted])
    assert os.path.exists(cloned)


def test_the_purge_never_removes_a_build_dir_that_is_also_the_source_tree(tmp_path):
    # a dep named after a platform build dir has src_dir == build_dir, and a remove would take the
    # working tree with every uncommitted change in it
    archives = [_archive('caf5158')]
    target = _target_with_cache(tmp_path, archives, shim=archives[0].filename[:-4])
    target.dep.src_dir = target.dep.build_dir
    up.purge_local(target, archives)
    assert os.path.exists(target.dep.build_dir)


def test_the_prompt_names_the_local_paths_it_will_delete(tmp_path):
    # approving `delete these archives` must not silently take the unpacked headers and libs as well
    archives = [_archive('caf5158')]
    target = _target_with_cache(tmp_path, archives, shim=archives[0].filename[:-4])
    text = up.describe_run({target: archives}, 'files.example.com')
    assert 'local copies this also removes' in text
    assert target.dep.build_dir in text and archives[0].filename in text


def test_the_purge_leaves_a_build_dir_that_is_not_a_shim(tmp_path):
    archives = [_archive('caf5158')]
    target = _target_with_cache(tmp_path, archives)
    up.purge_local(target, archives)
    assert os.path.exists(target.dep.build_dir)


# --- the whole command --------------------------------------------------------

def _run(tmp_path, selector, listed, assume_yes=True, is_pkg=False, keep=None, ftp=None, extra_targets=0):
    """Drive the whole run-level pass with the FTP session and the listing stubbed. `extra_targets`
    adds more targets to the one run, so a test can pin what is per-run and what is per-target."""
    targets = [_target_with_cache(tmp_path / str(i), listed) for i in range(1 + extra_targets)]
    config = targets[0].config
    config.artifactory_ftp = 'files.example.com'
    config.unpublish, config.unpublish_keep, config.assume_yes = selector, keep, assume_yes
    for target in targets:
        target.config = config
        target.dep.dep_source.is_pkg = is_pkg
    ftp = ftp or Mock()
    with patch.object(up, 'connect', return_value=ftp) as connect, \
         patch.object(up, 'list_archives', return_value=listed):
        deleted = up.unpublish_run(targets, config)
    return deleted, ftp, targets[0], connect


def test_a_confirmed_unpublish_deletes_every_matching_archive(tmp_path):
    listed = [_archive('caf5158'), _archive('caf5158', compiler='msvc14.51'), _archive('other')]
    deleted, ftp, _, _ = _run(tmp_path, 'caf5158', listed)
    assert deleted == 2
    names = sorted(c.args[0] for c in ftp.delete.call_args_list)
    assert names == sorted(f'{NAME}/{a.filename}' for a in listed[:2])


def test_a_refused_prompt_deletes_nothing(tmp_path):
    listed = [_archive('caf5158')]
    with patch.object(up, '_confirm', return_value=False):
        deleted, ftp, target, _ = _run(tmp_path, 'caf5158', listed, assume_yes=False)
    assert deleted == 0
    ftp.delete.assert_not_called()
    assert os.path.exists(os.path.join(target.dep.dep_dir, listed[0].filename))  # the cache stays too


def test_a_selector_that_matches_nothing_never_prompts(tmp_path):
    with patch.object(up, '_confirm', side_effect=AssertionError('must not prompt')):
        deleted, ftp, _, _ = _run(tmp_path, 'nosuchversion', [_archive('caf5158')])
    assert deleted == 0
    ftp.delete.assert_not_called()


@pytest.mark.parametrize('selector, keep, label', [
    ('nosuchversion', None, 'nosuchversion'),
    ('prune-old', None, 'prune-old=20'),   # the command line hides the count, so the report names it
    ('prune-old', 3, 'prune-old=3'),
])
def test_a_selector_that_matches_nothing_names_the_target_and_the_count(tmp_path, selector, keep, label):
    # two archives of one version: `prune-old` keeps versions, so an archive count alone misreads
    listed = [_archive('caf5158'), _archive('caf5158', platform='windows')]
    with patch.object(up, 'console') as printed:
        _run(tmp_path, selector, listed, keep=keep)
    report = '\n'.join(c.args[0] for c in printed.call_args_list)
    assert f'Nothing to unpublish on files.example.com: no archive matched `{label}`' in report
    assert f'{NAME: <16} 2 archive(s) in 1 version(s)' in report


def test_a_package_dep_refuses_to_unpublish(tmp_path):
    with patch.object(up, 'console') as printed:
        deleted, ftp, _, _ = _run(tmp_path, 'caf5158', [_archive('caf5158')], is_pkg=True)
    assert deleted == 0
    ftp.delete.assert_not_called()
    # the run DID reach the target and refused it, so it must not report an empty scope
    assert 'every target in scope is a read-only package' in printed.call_args[0][0]


def test_a_run_that_reached_no_target_says_so(tmp_path):
    config = _target_with_cache(tmp_path, []).config
    config.artifactory_ftp, config.unpublish = 'files.example.com', 'prune-all'
    with patch.object(up, 'connect'), patch.object(up, 'console') as printed:
        assert up.unpublish_run([], config) == 0
    assert 'the run reached no target' in printed.call_args[0][0]


def test_prune_old_with_a_keep_of_zero_deletes_every_version(tmp_path):
    # `0 or DEFAULT_KEEP` would silently keep 20, which is the opposite of what the user asked for
    listed = [_archive('a1'), _archive('b2')]
    deleted, _, _, _ = _run(tmp_path, 'prune-old', listed, keep=0)
    assert deleted == 2


def test_a_failed_delete_keeps_its_local_copy(tmp_path):
    # the zip is still on the server, so dropping the cache would re-download it for nothing
    listed = [_archive('caf5158')]
    refusing = Mock()
    refusing.delete.side_effect = RuntimeError('550 permission denied')
    deleted, _, target, _ = _run(tmp_path, 'caf5158', listed, ftp=refusing)
    assert deleted == 0
    assert os.path.exists(os.path.join(target.dep.dep_dir, listed[0].filename))


def test_an_undated_version_is_left_out_of_the_prune_order(tmp_path):
    # a server that refuses MDTM dates nothing, and such a version is neither pruned nor counted
    dated = _archive('dated', day='01')
    undated = up.Archive(f'{NAME}-linux-ubuntu24-gcc13.3-x64-release-undated.zip', '', 10)
    assert up.newest_first(up.group_by_version(NAME, [dated, undated])) == ['dated']


def test_undated_versions_never_evict_the_dated_ones(tmp_path):
    # sorting them newest filled the keep window with undated versions and pruned every real upload
    undated = [up.Archive(f'{NAME}-linux-ubuntu24-gcc{i}-x64-release-u{i:02}.zip', '', 1) for i in range(20)]
    dated = [_archive('v1', day='01'), _archive('v2', day='02'), _archive('v3', day='03')]
    assert up.select(NAME, undated + dated, 'prune-old', keep=20) == []


def test_a_prune_never_takes_the_version_this_checkout_needs(tmp_path):
    # deleting it leaves the tree pointing at a package that exists nowhere
    archives = [_archive(f'v{i:02}', day=f'{i + 1:02}') for i in range(25)]
    doomed = up.select(NAME, archives, 'prune-old', keep=20, protect='v00')
    assert 'v00' not in [up.archive_version(NAME, a.filename) for a in doomed]


def test_prune_all_really_takes_everything(tmp_path):
    # `all` means all: a user who typed it asked to wipe the target, current version included
    archives = [_archive('v1', day='01'), _archive('v2', day='02')]
    assert len(up.select(NAME, archives, 'prune-all')) == 2


def test_a_version_that_spells_a_keyword_still_names_itself(tmp_path):
    # a git tag may be called `prune-all`, and asking for it must not delete the whole history
    keyword, other = _archive('prune-all', day='01'), _archive('other', day='02')
    picked = up.select(NAME, [keyword, other], 'prune-all')
    assert [a.filename for a in picked] == [keyword.filename]


def test_a_prune_old_run_asks_for_the_current_version_and_spares_it(tmp_path):
    # the wiring, not just select(): prune-old must look the version up and pass it as protected
    listed = [_archive(f'v{i:02}', day=f'{i + 1:02}') for i in range(25)]
    with patch.object(up, 'current_version', return_value='v00') as current:
        deleted, ftp, _, _ = _run(tmp_path, 'prune-old', listed)
    current.assert_called()
    gone = [c.args[0] for c in ftp.delete.call_args_list]
    assert deleted == 4 and not any('release-v00.zip' in g for g in gone)


def test_a_prune_all_run_asks_for_no_protection(tmp_path):
    with patch.object(up, 'current_version', side_effect=AssertionError('must not protect')):
        deleted, _, _, _ = _run(tmp_path, 'prune-all', [_archive('v1'), _archive('v2')])
    assert deleted == 2


def test_one_prompt_and_one_ftp_session_cover_the_whole_run(tmp_path):
    # `mama all unpublish=` would otherwise ask once per target, and nobody reads the thirtieth question
    with patch.object(up, '_confirm', return_value=True) as confirm:
        _, _, _, connect = _run(tmp_path, 'caf5158', [_archive('caf5158')], assume_yes=False, extra_targets=1)
    confirm.assert_called_once()
    connect.assert_called_once()


# --- the scope: what a run is allowed to reach ---------------------------------

def _scoped(name, is_root, user_target):
    config = SimpleNamespace(user_target=user_target)
    return SimpleNamespace(name=name, config=config, dep=SimpleNamespace(is_root=is_root))


@pytest.mark.parametrize('user_target, root_in, named_in, other_in', [
    (None,     True,  False, False),   # no target named: the root alone
    ('all',    True,  True,  True),
    ('libfoo', False, True,  False),
])
def test_the_scope_follows_the_target_the_user_typed(user_target, root_in, named_in, other_in):
    assert up.in_scope(_scoped('root', True, user_target)) is root_in
    assert up.in_scope(_scoped('libfoo', False, user_target)) is named_in
    assert up.in_scope(_scoped('other', False, user_target)) is other_in


def test_a_bare_target_name_reaches_the_scope():
    # main.py deduces the bare word after the parse, so the constructor froze `user_target` too early
    config = BuildConfig(['libfoo', 'unpublish=prune-all'])
    set_target_from_unused_args(config)
    assert config.user_target == 'libfoo'
    assert up.in_scope(_scoped('libfoo', False, config.user_target)) is True


def test_an_update_cannot_widen_the_unpublish_to_the_whole_tree():
    # `mama update` rewrites config.target to `all`. Reading that would delete every version of every
    # dep in the graph, from a command line that named no target at all.
    config = BuildConfig(['update', 'unpublish=prune-all'])
    assert config.user_target is None
    config.target = 'all'  # what main.py does next
    assert up.in_scope(_scoped('other', False, config.user_target)) is False


# --- the selector parsing -----------------------------------------------------

@pytest.mark.parametrize('arg, selector, keep', [
    ('unpublish=current', 'current', None),
    ('unpublish=caf5158', 'caf5158', None),
    ('unpublish=prune-all', 'prune-all', None),
    ('unpublish=prune-old', 'prune-old', None),
    ('unpublish=prune-old=30', 'prune-old', 30),
    ('unpublish=prune-old=0', 'prune-old', 0),   # keep nothing, and `or` would read this as the default
])
def test_the_command_line_reads_every_selector(arg, selector, keep):
    config = BuildConfig([arg])
    assert config.unpublish == selector and config.unpublish_keep == keep


@pytest.mark.parametrize('arg', ['unpublish=', 'unpublish=prune-old=many', 'unpublish=caf5158=2'])
def test_a_malformed_selector_is_refused(arg):
    with pytest.raises(RuntimeError):
        BuildConfig([arg])


def test_a_bare_unpublish_names_the_selectors_it_wants(arg=None):
    # it used to fall through to the target name and die with `target='unpublish' not found`
    with pytest.raises(RuntimeError, match='unpublish needs a selector'):
        BuildConfig(['unpublish'])


def test_deps_only_cannot_combine_with_unpublish():
    # deps_only means act on the dependencies, and the unpublish scope names the target. Rather than
    # guess which one wins over a delete, refuse the pair.
    with pytest.raises(RuntimeError, match='deps_only cannot combine with unpublish'):
        BuildConfig(['deps_only', 'unpublish=prune-all'])


def test_a_clean_run_still_reaches_the_unpublish(tmp_path):
    # `clean_only` returns before the usual call site, so a whole run has to prove that path calls it
    with stub_loaders(lambda r: None), stub_runners(), \
         patch('mama.artifactory_unpublish.unpublish_run') as run:
        mamabuild(['clean', 'unpublish=prune-all'], source_dir=make_project_dir(tmp_path))
    run.assert_called_once()


def test_a_run_that_never_loads_a_tree_still_refuses_a_bare_unpublish(tmp_path):
    with pytest.raises(RuntimeError, match='unpublish needs a selector'):
        BuildConfig(['clean', 'unpublish'])


def test_a_run_that_does_not_unpublish_opens_no_ftp_session():
    from mama import main
    with patch('mama.artifactory_unpublish.unpublish_run', side_effect=AssertionError('must not run')):
        main.run_unpublish(BuildConfig(['build']), [])


@pytest.mark.parametrize('arg', ['yes', 'y'])
def test_yes_on_the_command_line_is_read(arg):
    assert BuildConfig([arg]).assume_yes is True
