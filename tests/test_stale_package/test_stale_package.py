"""Pins the `R` and `B` records of papa.txt, and that a package built against another ABI of a dep builds from source."""
import os, threading
from types import SimpleNamespace
from unittest.mock import Mock, patch
import pytest
from testutils import (FakeUnifiedDep, FakeWalkDep, make_includes_target, make_mock_dep, make_mock_local_dep, make_mock_shim_dep,
                       make_unified_config, make_walk_config, should_build_reasons)
import mama.artifactory as artifactory_mod
from mama import dependency_chain as dc
from mama.dependency_chain import load_dependency_chain, reload_stale_packages
from mama.artifactory import same_abi
from mama.papa_deploy import PapaFileInfo, current_identities, papa_deploy_to
from mama.types.git import Git
from mama.types.local_source import LocalSource
from mama.utils.fileio import read_text_from, write_text_to

OLD, NEW = 'ReCpp-linux-24-gcc14.2-x64-release-d292ca0', 'ReCpp-linux-24-gcc14.2-x64-release-83f5a3a'


def _fetched_shim(tmp_path, built_against=None, **config):
    """A shim whose package recorded ReCpp at OLD, in a build run unless `config` says otherwise."""
    dep = make_mock_shim_dep(tmp_path, write_papa_txt=True, **{'build': True, **config})
    dep.from_artifactory = True
    dep.artifactory_archive = 'libfoo-linux-24-gcc14.2-x64-release-abc1234'
    dep.built_against = {'ReCpp': OLD} if built_against is None else built_against
    return dep


def _child(name, archive, behind=False, version='', dirty=''):
    """A loaded source dep that would publish `archive`, '' when nothing names it yet. `dirty` is the
    fingerprint of an uncommitted edit."""
    dep = SimpleNamespace(name=name, artifactory_archive='', archive_name_memo=archive, already_loaded=True,
                          load_deferred=False, from_artifactory=False, get_children=lambda: [],
                          artifacts_behind_source=lambda: behind,
                          dep_source=SimpleNamespace(working_tree_fingerprint=lambda dep: dirty))
    dep.target = SimpleNamespace(dep=dep, version=version)
    return dep


def test_a_package_built_against_another_archive_of_a_dep_goes(tmp_path):
    dep = _fetched_shim(tmp_path)
    write_text_to(f'{dep.dep_dir}/{dep.artifactory_archive}.zip', 'zip')
    assert dep.reject_stale_package([_child('ReCpp', NEW)])
    assert dep.stale_package_cause == 'ReCpp changed'
    assert not dep.from_artifactory and not dep.artifactory_archive and not dep.built_against
    assert not dep.is_artifactory_shim()  # the next load clones the source
    assert not os.path.exists(dep.papa_package_file())  # else the next run unpacks the same package again
    assert os.path.exists(dep.archive_marker('libfoo-linux-24-gcc14.2-x64-release-abc1234', 'rejected'))
    assert not os.path.exists(f'{dep.dep_dir}/libfoo-linux-24-gcc14.2-x64-release-abc1234.zip')  # the cached download
    assert dep.did_check_artifactory and not dep.already_loaded
    assert not dep.has_usable_artifacts()  # the unpacked files stay on disk until a build replaces them


def test_a_reject_of_a_package_with_no_archive_name_writes_no_marker(tmp_path):
    # an old shim marker can lack the archive name, and a nameless marker matches no upload
    dep = _fetched_shim(tmp_path)
    dep.artifactory_archive = ''
    assert dep.reject_stale_package([_child('ReCpp', NEW)])
    with patch('mama.build_dependency.current_identities', autospec=True, return_value=[]):
        dep.save_dependency_list()
    assert not [f for f in os.listdir(dep.dep_dir) if f.endswith(('.stale', '.rejected'))]


def test_a_fetch_skips_an_archive_this_machine_rejected(tmp_path):
    # a first-time build after the reject would unpack it again, and its `D` records would name the children first
    dep = make_mock_dep(tmp_path)
    write_text_to(dep.archive_marker('libfoo-abc1234', 'rejected'), '')
    target = SimpleNamespace(dep=dep, config=dep.config, version='1.0.0')  # a version skips the mamafile read
    with patch.object(artifactory_mod, 'artifactory_archive_name', autospec=True, return_value='libfoo-abc1234'), \
         patch.object(artifactory_mod, '_fetch_package', autospec=True) as fetch:
        assert artifactory_mod.artifactory_fetch_and_reconfigure(target) == (False, None)
    fetch.assert_not_called()


def test_a_rejected_header_only_dep_still_packages(tmp_path):
    dep = _fetched_shim(tmp_path)
    dep.reject_stale_package([_child('ReCpp', NEW)])
    dep.nothing_to_build = True  # the source load reads it from the mamafile
    assert dep.has_usable_artifacts()  # else package() never runs, and the parents lose its include dirs


def test_the_rejected_dep_builds_and_names_the_dep_that_changed(tmp_path):
    dep = _fetched_shim(tmp_path)
    dep.reject_stale_package([_child('ReCpp', NEW)])
    built, warned = should_build_reasons(dep, loaded_from_pkg=False)
    assert built and 'stale package, ReCpp changed' in warned


@pytest.mark.parametrize('deps, built_against', [
    ([_child('ReCpp', OLD)], None),                              # the same archive the package built against
    ([_child('ReCpp', NEW, version='3.2.9')], {'ReCpp': '3.2.1'}),  # a patch release keeps the ABI
    ([_child('ReCpp', '')], None),                               # a dep nothing names yet
    ([], {}),                                                    # a leaf that predates the `B` record
])
def test_a_package_whose_deps_still_match_stays(tmp_path, deps, built_against):
    dep = _fetched_shim(tmp_path, built_against)
    assert not dep.reject_stale_package(deps)
    assert dep.from_artifactory and dep.is_artifactory_shim() and os.path.exists(dep.papa_package_file())


@pytest.mark.parametrize('child, built_against, cause', [
    (_child('ReCpp', NEW, version='3.3.0'), {'ReCpp': '3.2.1'}, 'ReCpp changed'),  # a minor release breaks the ABI
    (_child('ReCpp', OLD), {'ReCpp': '3.2.1'}, 'ReCpp changed'),          # the dep dropped its version
    (_child('ReCpp', OLD, dirty='f00d'), None, 'ReCpp changed'),          # an uncommitted edit keeps the archive name
    (_child('ReCpp', OLD), {'ReCpp': OLD + '+edit-f00d'}, 'ReCpp changed'),  # built against an edit, which is gone
    (_child('zlib', NEW), None, 'no record of zlib'),                     # a dep the package never recorded
    (_child('ReCpp', NEW), {}, 'no record of ReCpp'),                     # a package that predates the `B` record
    (_child('ReCpp', NEW, version='3.2.5'), {'ReCpp': '3.2.1', 'zlib': OLD}, 'zlib changed'),  # a patch dropped zlib
])
def test_a_package_with_an_unproven_dep_below_it_goes(tmp_path, child, built_against, cause):
    dep = _fetched_shim(tmp_path, built_against)
    assert dep.reject_stale_package([child]) and dep.stale_package_cause == cause


@pytest.mark.parametrize('built, now, same', [
    ('3.2.1', '3.2.9', True), ('3.2.1', '3.3.0', False), ('3.2.1', '4.2.1', False), (OLD, OLD, True),
    (OLD, NEW, False), ('3.2.1', OLD, False), ('34.0', '34.0', True), ('34.0', '34.1', False),
    (OLD + '+edit-f00d', OLD + '+edit-f00d', True), (OLD + '+edit-f00d', OLD, False),
])
def test_two_semvers_agree_on_major_minor_and_anything_else_matches_exactly(built, now, same):
    assert same_abi(built, now) == same


@pytest.mark.parametrize('config, rejects', [({'build': False}, False), ({'build': False, 'update': True}, True),
                                             ({'build': False, 'update': True, 'lock_generation': True}, False)])
def test_only_a_build_or_an_update_rejects_a_package(tmp_path, config, rejects):
    assert _fetched_shim(tmp_path, **config).reject_stale_package([_child('ReCpp', NEW)]) == rejects


def test_a_package_dep_has_no_source_so_it_warns_once(tmp_path):
    dep = _fetched_shim(tmp_path, print=True)
    dep.dep_source = SimpleNamespace(is_pkg=True)
    with patch('mama.build_dependency.warning') as warned:
        assert not dep.reject_stale_package([_child('ReCpp', NEW)])
        assert not dep.reject_stale_package([_child('ReCpp', NEW)])  # the pass after the walk asks again
    assert warned.call_count == 1 and 'no source to build' in str(warned.call_args)
    assert dep.from_artifactory


def test_a_source_dep_names_the_archive_its_source_would_publish_once():
    dep = _child('logging', None)
    dep.target = Mock()
    with patch('mama.artifactory.artifactory_archive_name', autospec=True, return_value='logging-local-3c5d') as name:
        assert artifactory_mod.current_archive_name(dep) == 'logging-local-3c5d'
        assert artifactory_mod.current_archive_name(dep) == 'logging-local-3c5d'
    name.assert_called_once_with(dep.target)  # a local dep hashes its whole tree for this


@pytest.mark.parametrize('attrs', [{'load_deferred': True}, {'already_loaded': False}])
def test_a_dep_that_did_not_load_has_no_archive_name(attrs):
    dep = _child('krattutil', '')
    dep.target = Mock()
    for k, v in attrs.items(): setattr(dep, k, v)
    with patch('mama.artifactory.artifactory_archive_name', autospec=True) as name:
        assert artifactory_mod.current_archive_name(dep) == ''
    name.assert_not_called()  # a deferred git dep would run an ls-remote


def test_the_load_after_a_reject_clones_the_source_and_builds_it(tmp_path):
    dep = make_mock_dep(tmp_path, build=True)
    def fetch(probe_target):
        probe_target.dep.from_artifactory = True
        probe_target.dep.built_against = {'ReCpp': OLD}
        return (True, [])
    with patch.object(Git, 'init_commit_hash', return_value='abc1234'), \
         patch.object(artifactory_mod, 'artifactory_fetch_and_reconfigure', side_effect=fetch) as fetch_mock, \
         patch.object(Git, 'dependency_checkout', return_value=False) as clone_mock:
        dep.load()
        assert dep.is_artifactory_shim() and not clone_mock.called
        assert dep.reject_stale_package([_child('ReCpp', NEW)])
        assert dep.load()
    fetch_mock.assert_called_once()
    clone_mock.assert_called_once()
    assert not dep.from_artifactory and dep.should_rebuild


@pytest.mark.parametrize('if_needed, rejected, built, uploads', [(True, False, False, False), (True, True, False, False),
                                                                 (True, True, True, True), (False, True, False, True)])
def test_an_if_needed_upload_replaces_only_an_archive_this_machine_rejected_and_rebuilt(tmp_path, if_needed, rejected,
                                                                                        built, uploads):
    # the upload is a later mama run, so only the marker on disk remembers the reject. A failed rebuild
    # leaves the old objects, which must not replace the copy on the server.
    dep = _fetched_shim(tmp_path)
    archive = dep.artifactory_archive
    if rejected: dep.reject_stale_package([_child('ReCpp', NEW)])
    with patch('mama.build_dependency.current_identities', autospec=True, return_value=[]):
        if built: dep.save_dependency_list()
    stale, rejected_marker = dep.archive_marker(archive, 'stale'), dep.archive_marker(archive, 'rejected')
    target = SimpleNamespace(name='libfoo', config=dep.config, dep=dep)
    dep.config.if_needed = if_needed
    with patch('ftplib.FTP_TLS'), patch.object(artifactory_mod, 'artifactory_ftp_login', autospec=True), \
         patch.object(artifactory_mod, 'artifact_already_exists', autospec=True, return_value=True), \
         patch.object(artifactory_mod, 'artifactory_upload', autospec=True) as upload:
        assert artifactory_mod.artifactory_upload_ftp(target, f'{tmp_path}/{archive}.zip') == uploads
    assert upload.called == uploads and not os.path.exists(stale)  # one replace, then if_needed skips again
    assert os.path.exists(rejected_marker) == (rejected and not built)  # only a rebuild made the server copy fit to fetch


def _target(tmp_path, dep_attrs=None, recpp=NEW, recpp_behind=False, recpp_version='', version=''):
    """A target over krattutil, which sits over ReCpp."""
    target = make_includes_target(str(tmp_path))
    child = Mock(artifactory_archive='krattutil-linux-24-gcc14.2-x64-release-main-c9ae1c1', from_artifactory=True,
                 package_version='')
    child.name = 'krattutil'  # Mock(name=..) names the mock itself, not the attribute
    child.target.dep = child
    child.dep_source.get_papa_string.return_value = 'git krattutil,url,main,,'
    child.dep_source.version_suffix = ''
    child.artifacts_behind_source.return_value = False
    child.get_children.return_value = [_child('ReCpp', recpp, recpp_behind, recpp_version)]
    target.children.return_value = [child]
    target.version, target.dep.package_version, target.dep.should_rebuild = version, '', False
    for k, v in (dep_attrs or {}).items(): setattr(target.dep, k, v)
    return target


def _deploy(tmp_path, record=None, **target) -> PapaFileInfo:
    """Deploy _target(**target) after a build that recorded `record`, and read the papa file back."""
    t = _target(tmp_path, **target)
    if record: write_text_to(f'{t.dep.build_dir}/mama_built_against', '\n'.join(f'{n} {i}' for n, i in record.items()))
    deploy_dir = str(tmp_path / 'deploy')
    os.makedirs(deploy_dir)
    papa_deploy_to(t, deploy_dir, r_includes=False, r_dylibs=False, r_syslibs=False, r_assets=False)
    return PapaFileInfo(os.path.join(deploy_dir, 'papa.txt'))


def _identities(tmp_path, **target) -> dict:
    return dict(current_identities(_target(tmp_path, **target)))


def test_the_identities_cover_every_dep_below_the_target(tmp_path):
    # ReCpp is not a direct child, and its inline code still sits inside these objects
    assert _identities(tmp_path) == {'krattutil': 'krattutil-linux-24-gcc14.2-x64-release-main-c9ae1c1', 'ReCpp': NEW}
    assert _identities(tmp_path / 'semver', recpp_version='3.2.1')['ReCpp'] == '3.2.1'


def test_a_deploy_records_the_semver_of_the_target(tmp_path):
    assert _deploy(tmp_path, version='1.4.0').version == '1.4.0'
    assert _deploy(tmp_path / 'plain', version='34.0').version == ''  # not a semver, so the archive name pins it


def test_a_deploy_writes_what_the_last_build_compiled_against_not_this_run(tmp_path):
    # `mama upload` builds nothing, so the objects still hold ReCpp 3.2 while this run has 3.3
    assert _deploy(tmp_path, {'ReCpp': '3.2.1'}, recpp_version='3.3.0').built_against == {'ReCpp': '3.2.1'}
    assert _deploy(tmp_path / 'unrecorded').built_against == {}  # an unknown ABI, which a consumer rejects
    # a build() hook that deploys runs before the record of this build exists
    rebuilt = _deploy(tmp_path / 'rebuilt', {'ReCpp': '3.2.1'}, recpp_version='3.3.0', dep_attrs={'should_rebuild': True})
    assert rebuilt.built_against['ReCpp'] == '3.3.0'


def test_a_fetched_package_deploys_the_records_it_came_with(tmp_path):
    papa = _deploy(tmp_path, dep_attrs={'from_artifactory': True, 'built_against': {'ReCpp': OLD}})
    assert papa.built_against == {'ReCpp': OLD}


def test_the_identity_marks_a_dep_whose_artifacts_differ_from_its_source(tmp_path):
    assert _identities(tmp_path, recpp_behind=True)['ReCpp'] == NEW + '+behind'
    assert 'ReCpp' not in _identities(tmp_path / 'nameless', recpp='', recpp_behind=True)


@pytest.mark.parametrize('recorded, is_root, changed', [('local-1', False, False), ('local-0', False, True),
                                                       (None, False, False), ('local-0', True, False)])
def test_a_local_dep_compares_its_content_version_with_the_one_its_build_recorded(tmp_path, recorded, is_root, changed):
    # a commit leaves no uncommitted edit, so only the content version shows that the subfolder moved. A build
    # that predates the record counts as current, so an upgrade rebuilds nothing.
    (tmp_path / 'src').mkdir()
    dep = make_mock_local_dep(tmp_path, tmp_path / 'src')
    dep.is_root = is_root
    if recorded: write_text_to(dep.dep_source.src_version_file(dep), recorded)
    with patch('mama.types.local_source.compute_version', autospec=True, return_value='local-1') as walk:
        assert dep.dep_source.content_version_changed(dep) == changed
        dep.dep_source.content_version_changed(dep)
    assert walk.call_count == (1 if recorded and not is_root else 0)  # one walk per dep and run


def test_a_committed_change_to_a_local_dep_rebuilds_it(tmp_path):
    (tmp_path / 'src').mkdir()
    dep = make_mock_local_dep(tmp_path, tmp_path / 'src')
    write_text_to(dep.dep_source.src_version_file(dep), 'local-0')
    with patch('mama.types.local_source.compute_version', autospec=True, return_value='local-1'), \
         patch('mama.types.local_source.source_walk_moved', autospec=True, return_value=True), \
         patch('mama.types.local_source.git_source_changed', autospec=True, return_value=False):
        assert dep.dep_source.source_tree_changed(dep) and dep.artifacts_behind_source()


@pytest.mark.parametrize('status, head, tree_changed, behind', [
    ('abc1234', 'abc1234', False, False),
    ('abc1234', 'def5678', False, True),   # the checkout moved, and nothing rebuilt the dep
    ('abc1234', 'abc1234', True, True),    # an uncommitted edit
    (None, 'abc1234', False, True),        # artifacts with no record of their source
    (None, None, False, False),            # no .git: a source copy has no commit to compare
])
def test_a_git_dep_compares_its_checkout_with_the_source_of_its_artifacts(tmp_path, status, head, tree_changed, behind):
    dep = make_mock_dep(tmp_path)
    if head: os.makedirs(f'{dep.src_dir}/.git')
    if status: write_text_to(dep.dep_source.git_status_file(dep), Git.format_git_status('url', '', 'main', status))
    with patch.object(Git, 'get_current_repository_commit', autospec=True, return_value=head), \
         patch.object(Git, 'source_tree_changed', autospec=True, return_value=tree_changed):
        assert dep.artifacts_behind_source() == behind


def test_only_a_dep_that_did_not_build_asks_its_source_once(tmp_path):
    dep = make_mock_dep(tmp_path)
    with patch.object(Git, 'artifacts_behind_source', autospec=True, return_value=True) as asked:
        dep.should_rebuild = True
        assert not dep.artifacts_behind_source()
        dep.should_rebuild = False
        assert dep.artifacts_behind_source() and dep.artifacts_behind_source()
    asked.assert_called_once()  # every parent deploy asks about each dep below it


def _source_dep(tmp_path, record=None, **config):
    """A source-built dep whose last build recorded `record` as the identities below it. None: no record."""
    dep = make_mock_dep(tmp_path, **{'build': True, **config})
    os.makedirs(dep.build_dir, exist_ok=True)
    lines = '\n'.join(f'{name} {identity}' for name, identity in (record or {}).items())
    if record is not None: write_text_to(f'{dep.build_dir}/mama_built_against', lines)
    return dep


@pytest.mark.parametrize('record, child, reason', [
    ({'ReCpp': '3.2.1'}, _child('ReCpp', NEW, version='3.3.0'), 'BUILD [ReCpp changed]'),
    (None, _child('ReCpp', NEW), 'BUILD [no record of ReCpp]'),  # a build that predates the record
])
def test_a_source_dep_rebuilds_when_a_dep_below_it_changed_its_abi(tmp_path, record, child, reason):
    dep = _source_dep(tmp_path, record, print=True)
    with patch('mama.build_dependency.warning') as warned, \
         patch('mama.build_dependency.current_archive_name', autospec=True, return_value='libfoo-abc1234'):
        assert dep.rebuild_if_stale_source([child])
    assert dep.should_rebuild and reason in str(warned.call_args)
    assert dep.stale_archive == 'libfoo-abc1234'  # the next if_needed upload replaces it, once the build succeeds


def test_a_source_dep_built_against_an_unchanged_edit_keeps_its_build(tmp_path):
    # the same uncommitted edit on both runs, so only a further edit rebuilds the parents
    dep = _source_dep(tmp_path, {'ReCpp': OLD + '+edit-f00d'})
    assert not dep.rebuild_if_stale_source([_child('ReCpp', OLD, dirty='f00d')])


def test_a_fetched_package_writes_no_build_record(tmp_path):
    # after_load can flag a fetched package whose child rebuilt, and its objects still came from the package
    dep = _fetched_shim(tmp_path)
    dep.save_dependency_list()
    assert not os.path.exists(f'{dep.build_dir}/mama_built_against')


def test_a_build_after_a_run_that_rejected_but_failed_still_marks_the_stale_archive(tmp_path):
    # that run wrote only `.rejected`, and this run fetches nothing, so nothing sets stale_archive
    dep = _source_dep(tmp_path)
    write_text_to(dep.archive_marker('libfoo-abc1234', 'rejected'), '')
    with patch('mama.build_dependency.current_archive_name', autospec=True, return_value='libfoo-abc1234'), \
         patch('mama.build_dependency.current_identities', autospec=True, return_value=[]):
        dep.save_dependency_list()
    assert os.path.exists(dep.archive_marker('libfoo-abc1234', 'stale'))


def test_a_broken_record_line_reads_as_no_record(tmp_path):
    assert _source_dep(tmp_path, {'ReCpp': '3.2.1 extra'}).rebuild_if_stale_source([_child('ReCpp', NEW, version='3.2.1')])


def test_a_source_dep_rebuilds_when_a_recorded_dep_is_no_longer_below_it(tmp_path):
    # the objects can still call the dep that went, so the link fails or takes another copy of it
    dep = _source_dep(tmp_path, {'ReCpp': '3.2.1', 'zlib': OLD})
    with patch('mama.build_dependency.current_archive_name', autospec=True, return_value=''):
        assert dep.rebuild_if_stale_source([_child('ReCpp', NEW, version='3.2.5')])


def test_a_source_dep_whose_record_still_matches_keeps_its_build(tmp_path):
    assert not _source_dep(tmp_path, {'ReCpp': '3.2.1'}).rebuild_if_stale_source([_child('ReCpp', NEW, version='3.2.9')])


@pytest.mark.parametrize('attrs, config', [({'from_artifactory': True}, {}), ({'should_rebuild': True}, {}),
                                           ({'nothing_to_build': True}, {}), ({'is_root': True}, {}),
                                           ({}, {'artifactory_ftp': ''}), ({}, {'build': False})])
def test_only_a_source_build_that_packages_can_reach_checks_its_record(tmp_path, attrs, config):
    dep = _source_dep(tmp_path, **config)  # no record, so a check would mark it
    for k, v in attrs.items(): setattr(dep, k, v)
    assert not dep.rebuild_if_stale_source([_child('ReCpp', NEW)])


@pytest.mark.parametrize('ftp, written', [('ftp.example.com', 'ReCpp 3.2.1'), ('', None)])
def test_a_build_records_the_identities_below_it_when_packages_exist(tmp_path, ftp, written):
    dep = _source_dep(tmp_path, {'ReCpp': '3.1.0'}, artifactory_ftp=ftp)  # without packages the old record goes
    with patch('mama.build_dependency.current_identities', autospec=True, return_value=[('ReCpp', '3.2.1')]):
        dep.save_dependency_list()
    record = f'{dep.build_dir}/mama_built_against'
    assert (read_text_from(record) if os.path.exists(record) else None) == written


def _rejects_once(dep, log, new_child=None, redeclared=None):
    """Make `dep` a package that the check rejects. `new_child` is a child its mamafile names and its package did not,
    and `redeclared` a child its mamafile names with another source."""
    dep.built_against = {'ReCpp': OLD}
    def reject(deps=None):
        if not dep.built_against: return False  # a dep is rejected once, like the real check
        log.append(('reject', dep.name, sorted(d.name for d in dc.get_flat_child_deps(dep))))
        dep.built_against = {}
        if new_child: dep._children.append(new_child)
        if redeclared: dep.redeclared = [redeclared.name]
        return True
    dep.reject_stale_package = reject


def test_the_classic_pass_reloads_a_rejected_dep_and_every_child_its_source_names():
    log = []; cfg = make_walk_config()
    recpp, extra = FakeWalkDep('ReCpp', cfg, log), FakeWalkDep('extra', cfg, log)
    dep = FakeWalkDep('krattutil', cfg, log, [recpp])
    _rejects_once(dep, log, new_child=extra)
    root = FakeWalkDep('root', cfg, log, [dep])
    root.after_load = lambda: log.append('root after_load')
    load_dependency_chain(root)
    reload_stale_packages(root)
    # ReCpp loads once. The root runs after_load again, so a source-built root relinks.
    assert log == ['root', 'krattutil', 'ReCpp', 'root after_load', ('reject', 'krattutil', ['ReCpp']),
                   'krattutil', 'extra', 'root after_load']


def test_the_classic_pass_checks_an_ancestor_again_after_a_reload_names_a_new_dep():
    log = []; cfg = make_walk_config()
    n, m, p = FakeWalkDep('N', cfg, log), FakeWalkDep('M', cfg, log), FakeWalkDep('P', cfg, log)
    _rejects_once(p, log, new_child=n)
    _rejects_once(n, log, new_child=m)  # only the second pass reaches N, after Q passed once
    q = FakeWalkDep('Q', cfg, log, [p])
    seen = []
    q.reject_stale_package = lambda deps=None: seen.append(sorted(d.name for d in dc.get_flat_child_deps(q))) or False
    root = FakeWalkDep('root', cfg, log, [q])
    load_dependency_chain(root)
    reload_stale_packages(root)
    assert seen[-1] == ['M', 'N', 'P']


def test_the_classic_pass_marks_a_stale_source_dep_and_flags_its_parents_again():
    log = []; cfg = make_walk_config()
    dep = FakeWalkDep('krattutil', cfg, log)
    dep.rebuild_if_stale_source = lambda deps=None: True
    root = FakeWalkDep('root', cfg, log, [dep])
    root.after_load = lambda: log.append('root after_load')
    load_dependency_chain(root)
    reload_stale_packages(root)
    assert log == ['root', 'krattutil', 'root after_load', 'root after_load']  # a source dep needs no reload


def test_the_scheduler_checks_a_source_dep_in_its_configure_after_every_child_built(no_cmake_writes):
    ev, lock = [], threading.Lock()
    cfg = make_unified_config()
    dep = FakeUnifiedDep('krattutil', cfg, ev, lock, shared_children=[FakeUnifiedDep('ReCpp', cfg, ev, lock)])
    dep.rebuild_if_stale_source = lambda deps=None: ev.append(('source check', 'krattutil'))
    dc.execute_unified(FakeUnifiedDep('root', cfg, ev, lock, shared_children=[dep]))
    assert ev.index(('bld', 'ReCpp')) < ev.index(('source check', 'krattutil')) < ev.index(('cfg', 'krattutil'))


def test_the_scheduler_reloads_a_stale_dep_in_its_configure_after_every_child_built(no_cmake_writes):
    ev, lock = [], threading.Lock()
    cfg = make_unified_config()
    recpp = FakeUnifiedDep('ReCpp', cfg, ev, lock)
    dep = FakeUnifiedDep('krattutil', cfg, ev, lock, shared_children=[recpp])
    _rejects_once(dep, ev)
    dc.execute_unified(FakeUnifiedDep('root', cfg, ev, lock, shared_children=[dep]))
    at = ev.index(('reject', 'krattutil', ['ReCpp']))
    assert ev.index(('bld', 'ReCpp')) < at < ev.index(('cfg', 'krattutil'))
    assert ('load', 'krattutil') in ev[at:]  # the source load runs inside the configure job


def test_the_scheduler_fails_when_the_source_redeclares_a_child_that_already_built(no_cmake_writes):
    # the child keeps its first declaration, so a build now would link the old ReCpp
    ev, lock = [], threading.Lock()
    cfg = make_unified_config()
    recpp = FakeUnifiedDep('ReCpp', cfg, ev, lock)
    dep = FakeUnifiedDep('krattutil', cfg, ev, lock, shared_children=[recpp])
    _rejects_once(dep, ev, redeclared=recpp)
    with patch('mama.dependency_chain._handle_failure') as failure, pytest.raises(SystemExit):
        dc.execute_unified(FakeUnifiedDep('root', cfg, ev, lock, shared_children=[dep]))
    assert 'names ReCpp other than its stale package did' in str(failure.call_args.args[1].error)


def test_the_classic_pass_stops_when_the_source_redeclares_a_child():
    log = []; cfg = make_walk_config(verbose=False)
    recpp = FakeWalkDep('ReCpp', cfg, log)
    dep = FakeWalkDep('krattutil', cfg, log, [recpp])
    _rejects_once(dep, log, redeclared=recpp)
    root = FakeWalkDep('root', cfg, log, [dep])
    load_dependency_chain(root)
    with patch('mama.dependency_chain._report_error') as report, pytest.raises(SystemExit):
        reload_stale_packages(root)
    assert 'names ReCpp other than its stale package did' in str(report.call_args.args[0])


@pytest.mark.parametrize('declared, redeclared', [({}, False), ({'tag': 'v2'}, True), ({'branch': 'dev'}, True),
                                                  ({'args': []}, True), ({'args': ['asan']}, True),
                                                  ({'version_suffix': '2'}, True)])  # the `V` record names a new recipe
def test_the_source_load_of_a_rejected_package_must_name_each_child_like_its_d_record(tmp_path, declared, redeclared):
    # the `D` record passed `lgpl`, so dropping it counts as much as adding `asan`
    dep = _fetched_shim(tmp_path)
    recpp = lambda **over: Git(**{'name': 'ReCpp', 'url': 'https://example.com/ReCpp.git', 'branch': 'main', 'tag': '',
                                  'mamafile': None, 'shallow': True, 'args': ['lgpl'], **over})
    dep.package_declarations = {'ReCpp': recpp().declaration()}
    assert dep.reject_stale_package([_child('ReCpp', NEW)])
    dep.add_child(recpp(**declared))
    assert dep.redeclared == (['ReCpp'] if redeclared else [])


def test_a_child_outside_a_stale_reload_is_never_redeclared(tmp_path):
    # a conflict between two other parents must not stop the reload of a third one
    dep = make_mock_dep(tmp_path)
    dep.add_child(Git(name='ReCpp', url='https://example.com/ReCpp.git', branch='', tag='v2', mamafile=None,
                      shallow=True, args=[]))
    assert dep.redeclared == []


def test_the_scheduler_fails_when_the_source_names_a_child_no_job_knows(no_cmake_writes):
    ev, lock = [], threading.Lock()
    cfg = make_unified_config()
    dep = FakeUnifiedDep('krattutil', cfg, ev, lock, shared_children=[])
    _rejects_once(dep, ev, new_child=FakeUnifiedDep('extra', cfg, ev, lock))
    with patch('mama.dependency_chain._handle_failure') as failure, pytest.raises(SystemExit):
        dc.execute_unified(FakeUnifiedDep('root', cfg, ev, lock, shared_children=[dep]))
    assert 'names extra' in str(failure.call_args.args[1].error)
    assert ('cfg', 'krattutil') not in ev
