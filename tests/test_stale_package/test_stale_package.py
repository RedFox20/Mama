"""Pins the `R` and `B` records of papa.txt, and that a package built against another ABI of a dep builds from source."""
import os, threading
from types import SimpleNamespace
from unittest.mock import Mock, patch
import pytest
from testutils import (FakeUnifiedDep, FakeWalkDep, make_includes_target, make_mock_dep, make_mock_shim_dep,
                       make_unified_config, make_walk_config, should_build_reasons)
import mama.artifactory as artifactory_mod
from mama import dependency_chain as dc
from mama.dependency_chain import load_dependency_chain, reload_stale_packages
from mama.artifactory import same_abi
from mama.papa_deploy import PapaFileInfo, papa_deploy_to
from mama.types.git import Git
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
    assert dep.reject_stale_package([_child('ReCpp', NEW)])
    assert dep.stale_package_cause == 'ReCpp changed'
    assert not dep.from_artifactory and not dep.artifactory_archive and not dep.built_against
    assert not dep.is_artifactory_shim()  # the next load clones the source
    assert not os.path.exists(dep.papa_package_file())  # else the next run unpacks the same package again
    assert dep.did_check_artifactory and not dep.already_loaded
    assert not dep.has_usable_artifacts()  # the unpacked files stay on disk until a build replaces them


def test_a_reject_of_a_package_with_no_archive_name_writes_no_marker(tmp_path):
    # an old shim marker can lack the archive name, and a nameless marker matches no upload
    dep = _fetched_shim(tmp_path)
    dep.artifactory_archive = ''
    assert dep.reject_stale_package([_child('ReCpp', NEW)])
    assert not [f for f in os.listdir(dep.dep_dir) if f.endswith('.stale')]


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


@pytest.mark.parametrize('rejected, uploads', [(False, False), (True, True)])
def test_an_if_needed_upload_replaces_only_an_archive_this_machine_rejected(tmp_path, rejected, uploads):
    # the upload is a later mama run, so only the marker on disk remembers the reject
    dep = _fetched_shim(tmp_path)
    archive = dep.artifactory_archive
    if rejected: dep.reject_stale_package([_child('ReCpp', NEW)])
    marker = dep.stale_archive_marker(archive)
    target = SimpleNamespace(name='libfoo', config=dep.config, dep=dep)
    dep.config.if_needed = True
    with patch('ftplib.FTP_TLS'), patch.object(artifactory_mod, 'artifactory_ftp_login', autospec=True), \
         patch.object(artifactory_mod, 'artifact_already_exists', autospec=True, return_value=True), \
         patch.object(artifactory_mod, 'artifactory_upload', autospec=True) as upload:
        assert artifactory_mod.artifactory_upload_ftp(target, f'{tmp_path}/{archive}.zip') == uploads
    assert upload.called == uploads and not os.path.exists(marker)  # one replace, then if_needed skips again


def _deploy(tmp_path, dep_attrs=None, recpp=NEW, recpp_behind=False, recpp_version='', version='') -> PapaFileInfo:
    """Deploy a target over krattutil, which sits over ReCpp, and read the papa file back."""
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
    target.version, target.dep.package_version = version, ''
    for k, v in (dep_attrs or {}).items(): setattr(target.dep, k, v)
    deploy_dir = str(tmp_path / 'deploy')
    os.makedirs(deploy_dir)
    papa_deploy_to(target, deploy_dir, r_includes=False, r_dylibs=False, r_syslibs=False, r_assets=False)
    return PapaFileInfo(os.path.join(deploy_dir, 'papa.txt'))


def test_a_deploy_records_the_archive_of_every_dep_below_it(tmp_path):
    # ReCpp is not a direct child, and its inline code still sits inside these objects
    assert _deploy(tmp_path).built_against == {'krattutil': 'krattutil-linux-24-gcc14.2-x64-release-main-c9ae1c1',
                                               'ReCpp': NEW}


def test_a_deploy_records_the_semver_of_the_target_and_of_each_dep_that_has_one(tmp_path):
    papa = _deploy(tmp_path, recpp_version='3.2.1', version='1.4.0')
    assert papa.version == '1.4.0' and papa.built_against['ReCpp'] == '3.2.1'
    assert _deploy(tmp_path / 'plain', version='34.0').version == ''  # not a semver, so the archive name pins it


def test_a_fetched_package_deploys_the_records_it_came_with(tmp_path):
    papa = _deploy(tmp_path, {'from_artifactory': True, 'built_against': {'ReCpp': OLD}})
    assert papa.built_against == {'ReCpp': OLD}


def test_a_deploy_marks_a_dep_whose_artifacts_differ_from_its_source(tmp_path):
    assert _deploy(tmp_path, recpp_behind=True).built_against['ReCpp'] == NEW + '+behind'
    assert 'ReCpp' not in _deploy(tmp_path / 'nameless', recpp='', recpp_behind=True).built_against


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
    with patch('mama.build_dependency.warning') as warned:
        assert dep.rebuild_if_stale_source([child])
    assert dep.should_rebuild and reason in str(warned.call_args)


def test_a_source_dep_built_against_an_unchanged_edit_keeps_its_build(tmp_path):
    # the same uncommitted edit on both runs, so only a further edit rebuilds the parents
    dep = _source_dep(tmp_path, {'ReCpp': OLD + '+edit-f00d'})
    assert not dep.rebuild_if_stale_source([_child('ReCpp', OLD, dirty='f00d')])


def test_a_broken_record_line_reads_as_no_record(tmp_path):
    assert _source_dep(tmp_path, {'ReCpp': '3.2.1 extra'}).rebuild_if_stale_source([_child('ReCpp', NEW, version='3.2.1')])


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
    dep = _source_dep(tmp_path, artifactory_ftp=ftp)
    with patch('mama.build_dependency.built_against', autospec=True, return_value=[('ReCpp', '3.2.1')]):
        dep.save_dependency_list()
    record = f'{dep.build_dir}/mama_built_against'
    assert (read_text_from(record) if os.path.exists(record) else None) == written


def _rejects_once(dep, log, new_child=None):
    """Make `dep` a package that the check rejects. `new_child` is a child its mamafile names and its package did not."""
    dep.built_against = {'ReCpp': OLD}
    def reject(deps=None):
        log.append(('reject', dep.name, sorted(d.name for d in dc.get_flat_child_deps(dep))))
        dep.built_against = {}
        if new_child: dep._children.append(new_child)
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


def test_the_scheduler_fails_when_the_source_names_a_child_no_job_knows(no_cmake_writes):
    ev, lock = [], threading.Lock()
    cfg = make_unified_config()
    dep = FakeUnifiedDep('krattutil', cfg, ev, lock, shared_children=[])
    _rejects_once(dep, ev, new_child=FakeUnifiedDep('extra', cfg, ev, lock))
    with patch('mama.dependency_chain._handle_failure') as failure, pytest.raises(SystemExit):
        dc.execute_unified(FakeUnifiedDep('root', cfg, ev, lock, shared_children=[dep]))
    assert 'names extra' in str(failure.call_args.args[1].error)
    assert ('cfg', 'krattutil') not in ev
