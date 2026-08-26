import json, os
from unittest.mock import DEFAULT, Mock, patch

import pytest
from testutils import git_run, make_git_and_mock_dep, make_mock_dep, make_mock_shim_dep, native_platform_name

from mama.dependency_lock import (DependencyLock, LockEntry, LockGeneration, LockSelector, _entry, _parse_args,
                                  _validate_checkout, read_lock, run_lock)
from mama.main import mamabuild
from mama.utils.fileio import remove_tree
from mama.types.git import Git, convert_git_url
from mama.utils.paths import normalized_path


def test_full_local_commit_returns_empty_when_git_cannot_start():
    source, dep = make_git_and_mock_dep()
    with patch('mama.types.git.execute_piped', return_value=None):
        assert source._full_local_commit(dep.src_dir, 'HEAD') == ''


def git(cwd, *args) -> str:
    result = git_run(args, cwd)
    result.check_returncode()
    return result.stdout.strip()


def commit(repo, files: dict[str, str], message: str) -> str:
    for name, contents in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding='utf-8')
    git(repo, 'add', '-A')
    git(repo, 'commit', '-q', '-m', message)
    return git(repo, 'rev-parse', 'HEAD')


def remote(tmp_path, name: str, mamafile: str) -> tuple[str, object, str]:
    work = tmp_path / f'{name}-work'
    work.mkdir()
    git(work, 'init', '-q', '-b', 'main')
    head = commit(work, {'mamafile.py': mamafile}, 'initial')
    bare = tmp_path / f'{name}.git'
    git(tmp_path, 'clone', '--bare', '-q', str(work), str(bare))
    return f'file:///{normalized_path(str(bare)).lstrip("/")}', work, head


def push(work, bare, files: dict[str, str], message: str) -> str:
    head = commit(work, files, message)
    git(work, 'push', str(bare), 'main')
    return head


def dep_mamafile(children: list[tuple[str, str]] = None) -> str:
    additions = '\n'.join(f"        self.add_git('{name}', '{url}', git_branch='main')"
                          for name, url in (children or [])) or '        pass'
    return f"""import mama

class dependency(mama.BuildTarget):
    def settings(self):
        self.nothing_to_build()

    def dependencies(self):
{additions}
"""


def root_mamafile(lines: list[str]) -> str:
    dependencies = '\n'.join(f'        {line}' for line in lines)
    return f"""import mama

class root(mama.BuildTarget):
    local_workspace = 'packages'

    def settings(self):
        self.nothing_to_build()

    def dependencies(self):
{dependencies}
"""


def write_mamafile(project, lines):
    project.joinpath('mamafile.py').write_text(root_mamafile(lines), encoding='utf-8')


def make_project(tmp_path, lines):
    project = tmp_path / 'project'
    project.mkdir()
    write_mamafile(project, lines)
    return project


def platform_dep(platform, url):
    return f"if self.{platform}: self.add_git('{platform}-dep', '{url}', git_branch='main')"


BUILDING_DEP = """import mama
import os

class dep(mama.BuildTarget):
    def build(self):
        with open(self.source_dir('version.txt')) as src:
            value = src.read()
        if os.path.exists(self.source_dir('fail-build')):
            raise RuntimeError('intentional build failure')
        with open(self.build_dir('libdep.a'), 'w') as out:
            out.write(value)

    def package(self):
        fail_once = self.source_dir('fail-package-once')
        failed = self.build_dir('.failed-package-once')
        if os.path.exists(fail_once) and not os.path.exists(failed):
            with open(self.build_dir('libdep.a'), 'w') as artifact:
                artifact.write('BROKEN')
            with open(failed, 'w') as marker:
                marker.write('failed')
            raise RuntimeError('intentional package failure')
        self.no_export_includes()
        self.export_lib('libdep.a')

    def deploy(self):
        self.papa_deploy(self.build_dir('deploy'))

    def test(self, args):
        with open(self.build_dir('libdep.a')) as artifact, open(self.source_dir('version.txt')) as source:
            assert artifact.read() == source.read()

    start = test
"""


def read(path) -> dict:
    return json.loads(path.read_text(encoding='utf-8'))


def by_name(path) -> dict:
    return {entry['name']: entry for entry in read(path)['dependencies']}


def lock_entry(**overrides):
    entry = {'name': 'dep', 'repository': 'example.com/org/dep',
             'selector': {'kind': 'branch', 'value': 'main'}, 'commit': 'a' * 40}
    entry.update(overrides)
    return entry


def lock_data(**overrides):
    data = {'format': 1, 'platforms': ['linux'], 'dependencies': []}
    data.update(overrides)
    return data


def lock_generation_dep(tmp_path):
    source, dep = make_git_and_mock_dep()
    generation = LockGeneration(str(tmp_path), ('linux',), None, None, None)
    generation.apply(source)
    return generation, source, dep


def write_uncovered_lock(project):
    platform = 'windows' if native_platform_name() != 'windows' else 'linux'
    project.joinpath('mama.lock').write_text(json.dumps(lock_data(platforms=[platform])), encoding='utf-8')


class ParentPath:
    def __init__(self, source_dir):
        self.source_dir = source_dir

    def path_relative_to_us(self, relative_path):
        return normalized_path(os.path.join(self.source_dir, relative_path))


_UNKNOWN_OPTION = (['lock', 'platforms=linux', 'bad=1'], 'Unknown lock option')
_TWO_TARGETS = (['lock', 'one', 'two', 'platforms=linux'], 'one dependency name')
_COMMIT_WITHOUT_TARGET = (['lock', 'commit=deadbeef', 'platforms=linux'], 'requires a dependency name')
_INVALID_COMMIT = (['lock', 'dep', 'commit=invalid', 'platforms=linux'], '7 to 40 hexadecimal')
_MISSING_PLATFORMS = (['lock'], 'needs platforms')


@pytest.mark.parametrize('args,error', [_UNKNOWN_OPTION, _TWO_TARGETS, _COMMIT_WITHOUT_TARGET,
                                        _INVALID_COMMIT, _MISSING_PLATFORMS])
def test_lock_rejects_invalid_arguments(args, error):
    with pytest.raises(RuntimeError, match=error):
        _parse_args(args)


_EMPTY_ENTRY = ({}, 'invalid dependency entry')
_EMPTY_NAME = (lock_entry(name=''), 'names and repositories')
_UNKNOWN_SELECTOR = (lock_entry(selector={'kind': 'other', 'value': 'main'}), 'unknown selector kind')
_EMPTY_SELECTOR = (lock_entry(selector={'kind': 'branch', 'value': ''}), 'empty branch selector')
_SHORT_COMMIT = (lock_entry(commit='abc'), 'invalid commit')


@pytest.mark.parametrize('raw,error', [_EMPTY_ENTRY, _EMPTY_NAME, _UNKNOWN_SELECTOR, _EMPTY_SELECTOR, _SHORT_COMMIT])
def test_lock_rejects_invalid_dependency_entries(raw, error):
    with pytest.raises(RuntimeError, match=error):
        _entry(raw)


_BAD_JSON = ('{', 'Could not read')
_BAD_ROOT = ('[]', 'root must be an object')
_BAD_FORMAT = (json.dumps(lock_data(format=2)), 'needs format 1')
_BAD_PLATFORMS = (json.dumps(lock_data(platforms=[''])), 'platforms must be a list of names')
_BAD_DEPENDENCIES = (json.dumps(lock_data(dependencies={})), 'dependencies must be a list')


@pytest.mark.parametrize('contents,error', [_BAD_JSON, _BAD_ROOT, _BAD_FORMAT, _BAD_PLATFORMS, _BAD_DEPENDENCIES])
def test_lock_reader_rejects_invalid_schema(tmp_path, contents, error):
    tmp_path.joinpath('mama.lock').write_text(contents, encoding='utf-8')
    with pytest.raises(RuntimeError, match=error):
        read_lock(str(tmp_path))


def test_lock_reader_requires_a_file_for_targeted_refresh(tmp_path):
    with pytest.raises(RuntimeError, match='does not exist'):
        read_lock(str(tmp_path), required=True)


def test_lock_generation_rejects_artifactory_only_dependency(tmp_path):
    project = make_project(tmp_path, ["self.set_artifactory_ftp('ftp://example')",
                                      "self.add_artifactory_pkg('dep')"])
    with pytest.raises(RuntimeError, match='Artifactory-only dependency dep'):
        run_lock(['lock', f'platforms={native_platform_name()}', 'silent'], str(project))


def test_targeted_lock_rejects_an_unknown_dependency(tmp_path):
    project = make_project(tmp_path, ['pass'])
    platform = native_platform_name()
    run_lock(['lock', f'platforms={platform}', 'silent'], str(project))
    with pytest.raises(RuntimeError, match="dependency 'missing' was not found"):
        run_lock(['lock', 'missing', f'platforms={platform}', 'silent'], str(project))


def test_parent_relative_mamafile_spellings_are_the_same_declaration(tmp_path):
    root = tmp_path / 'project'
    service = root / 'modules' / 'KrattlinkService'
    override = root / 'mamadeps' / 'android_openssl.py'
    override.parent.mkdir(parents=True)
    override.write_text('', encoding='utf-8')

    entry = LockEntry('android_openssl', 'github.com/kdab/android_openssl',
                      LockSelector('branch', 'master'), 'a' * 40)
    lock = DependencyLock('', ('android',), {'android_openssl': entry})
    root_decl = Git('android_openssl', 'https://github.com/KDAB/android_openssl.git',
                    'master', '', 'mamadeps/android_openssl.py', True, [])
    service_decl = Git('android_openssl', 'git@github.com:KDAB/android_openssl.git',
                       'master', '', '../../mamadeps/android_openssl.py', True, [])

    lock.apply(root_decl, ParentPath(root))
    lock.apply(service_decl, ParentPath(service))

    assert root_decl.locked_commit == service_decl.locked_commit == 'a' * 40


def test_lock_rejects_conflicting_declarations():
    entry = LockEntry('dep', 'example.com/org/dep', LockSelector('branch', 'main'), 'a' * 40)
    lock = DependencyLock('', ('linux',), {'dep': entry})
    first = Git('dep', 'https://example.com/org/dep.git', 'main', '', 'first.py', True, [])
    second = Git('dep', 'https://example.com/org/dep.git', 'main', '', 'second.py', True, [])
    lock.apply(first)
    with pytest.raises(RuntimeError, match='conflicting declarations'):
        lock.apply(second)


def test_lock_record_rejects_a_head_change(tmp_path):
    generation, source, dep = lock_generation_dep(tmp_path)
    source.locked_commit = 'a' * 40
    with patch.object(source, '_full_local_commit', return_value='b' * 40), \
         pytest.raises(RuntimeError, match='checkout HEAD changed'):
        generation.record(dep)


def test_lock_record_rejects_inconsistent_commits(tmp_path):
    generation, source, dep = lock_generation_dep(tmp_path)
    source.locked_commit = 'a' * 40
    with patch.object(source, '_full_local_commit', return_value=source.locked_commit):
        generation.record(dep)
    source.locked_commit = 'b' * 40
    with patch.object(source, '_full_local_commit', return_value=source.locked_commit), \
         pytest.raises(RuntimeError, match='resolved inconsistently'):
        generation.record(dep)


def test_checkout_validation_accepts_transport_override_with_custom_port():
    declared = 'ssh://git@example.com:2222/x/y.git'
    git_source, dep = make_git_and_mock_dep(url=declared)
    git_source.url = convert_git_url(declared, 'https')

    with patch.object(git_source, '_is_repo_broken', return_value=False), \
         patch('mama.dependency_lock.execute_piped', return_value='https://example.com/x/y.git'):
        _validate_checkout(dep, git_source)


def test_checkout_validation_accepts_absolute_origin_for_relative_local_url(tmp_path):
    cwd = str(tmp_path)
    declared = '.pytest_tmp/dep.git'
    origin = normalized_path(os.path.join(cwd, declared))
    git_source, dep = make_git_and_mock_dep(url=declared)
    with patch.object(git_source, '_is_repo_broken', return_value=False), \
         patch('mama.dependency_lock.os.getcwd', return_value=cwd), \
         patch('mama.dependency_lock.execute_piped', return_value=origin):
        _validate_checkout(dep, git_source)


def test_checkout_validation_resolves_relative_origin_from_checkout(tmp_path):
    cwd = tmp_path / 'project'
    checkout = tmp_path / 'packages' / 'dep' / 'checkout'
    repository = tmp_path / 'packages' / 'dep' / 'origin.git'
    git_source, dep = make_git_and_mock_dep(url=normalized_path(str(repository)))
    dep.src_dir = normalized_path(str(checkout))
    with patch.object(git_source, '_is_repo_broken', return_value=False), \
         patch('mama.dependency_lock.os.getcwd', return_value=str(cwd)), \
         patch('mama.dependency_lock.execute_piped', return_value='../origin.git'):
        _validate_checkout(dep, git_source)


def test_checkout_validation_rejects_relative_urls_with_different_bases(tmp_path):
    cwd = tmp_path / 'project'
    checkout = tmp_path / 'packages' / 'dep' / 'checkout'
    git_source, dep = make_git_and_mock_dep(url='../origin.git')
    dep.src_dir = normalized_path(str(checkout))
    with patch.object(git_source, '_is_repo_broken', return_value=False), \
         patch('mama.dependency_lock.os.getcwd', return_value=str(cwd)), \
         patch('mama.dependency_lock.execute_piped', return_value='../origin.git'), \
         pytest.raises(RuntimeError, match='does not match'):
        _validate_checkout(dep, git_source)


def test_clean_does_not_validate_stale_child_declarations(tmp_path):
    dep = make_mock_dep(tmp_path)
    dep.config.clean_only.return_value = True
    dep.config.dependency_lock = Mock()
    dep.add_child(Git('child', 'https://example.com/child.git', 'main', '', None, True, []))
    dep.config.dependency_lock.apply.assert_not_called()


def test_full_lock_is_deterministic_and_covers_platform_union(tmp_path):
    linux_url, _, linux_head = remote(tmp_path, 'linux-dep', dep_mamafile())
    windows_url, _, windows_head = remote(tmp_path, 'windows-dep', dep_mamafile())
    android_url, _, android_head = remote(tmp_path, 'android-dep', dep_mamafile())
    project = make_project(tmp_path, [platform_dep('linux', linux_url), platform_dep('windows', windows_url),
                                      platform_dep('android', android_url)])

    args = ['lock', 'platforms=linux,windows,android', 'silent']
    run_lock(args, str(project))
    first = project.joinpath('mama.lock').read_text(encoding='utf-8')
    run_lock(args, str(project))

    assert project.joinpath('mama.lock').read_text(encoding='utf-8') == first
    assert read(project / 'mama.lock')['platforms'] == ['android', 'linux', 'windows']
    entries = by_name(project / 'mama.lock')
    assert {name: entry['commit'] for name, entry in entries.items()} == {
        'android-dep': android_head, 'linux-dep': linux_head, 'windows-dep': windows_head}


def test_full_lock_refreshes_remote_head(tmp_path):
    url, work, old = remote(tmp_path, 'dep', dep_mamafile())
    project = make_project(tmp_path, [f"self.add_git('dep', '{url}')"])
    run_lock(['lock', 'platforms=linux', 'silent'], str(project))
    assert by_name(project / 'mama.lock')['dep']['commit'] == old

    new = push(work, tmp_path / 'dep.git', {'new.txt': 'new\n'}, 'advance default branch')
    run_lock(['lock', 'platforms=linux', 'silent'], str(project))

    assert by_name(project / 'mama.lock')['dep']['commit'] == new


def test_full_lock_refresh_updates_submodules_after_head_advances(tmp_path, monkeypatch):
    for key, value in (('GIT_CONFIG_COUNT', '1'), ('GIT_CONFIG_KEY_0', 'protocol.file.allow'),
                       ('GIT_CONFIG_VALUE_0', 'always')):
        monkeypatch.setenv(key, value)

    sub_work = tmp_path / 'sub-work'
    sub_work.mkdir()
    git(sub_work, 'init', '-q', '-b', 'main')
    sub_old = commit(sub_work, {'version.txt': 'old\n'}, 'initial submodule')
    sub_bare = tmp_path / 'sub.git'
    git(tmp_path, 'clone', '--bare', '-q', str(sub_work), str(sub_bare))

    dep_work = tmp_path / 'dep-work'
    dep_work.mkdir()
    git(dep_work, 'init', '-q', '-b', 'main')
    commit(dep_work, {'mamafile.py': dep_mamafile()}, 'initial dependency')
    git(dep_work, 'submodule', 'add', '-q', str(sub_bare), 'sub')
    dep_old = commit(dep_work, {}, 'add submodule')
    dep_bare = tmp_path / 'dep.git'
    git(tmp_path, 'clone', '--bare', '-q', str(dep_work), str(dep_bare))
    dep_url = f'file:///{normalized_path(str(dep_bare)).lstrip("/")}'

    project = make_project(tmp_path, [f"self.add_git('dep', '{dep_url}')"])
    run_lock(['lock', 'platforms=linux', 'silent'], str(project))
    checkout = project / 'packages' / 'dep' / 'dep'
    assert git(checkout, 'rev-parse', 'HEAD') == dep_old
    assert git(checkout / 'sub', 'rev-parse', 'HEAD') == sub_old

    sub_new = push(sub_work, sub_bare, {'version.txt': 'new\n'}, 'advance submodule')
    git(dep_work / 'sub', 'fetch', '-q', 'origin', 'main')
    git(dep_work / 'sub', 'checkout', '-q', sub_new)
    dep_new = push(dep_work, dep_bare, {}, 'advance submodule pointer')

    run_lock(['lock', 'platforms=linux', 'silent'], str(project))

    assert by_name(project / 'mama.lock')['dep']['commit'] == dep_new
    assert git(checkout, 'rev-parse', 'HEAD') == dep_new
    assert git(checkout / 'sub', 'rev-parse', 'HEAD') == sub_new


def test_targeted_refresh_preserves_unrelated_dependency(tmp_path):
    selected_url, selected_work, selected_old = remote(tmp_path, 'selected', dep_mamafile())
    other_url, other_work, other_old = remote(tmp_path, 'other', dep_mamafile())
    project = make_project(tmp_path, [f"self.add_git('selected', '{selected_url}', git_branch='main')",
                                      f"self.add_git('other', '{other_url}', git_branch='main')"])
    run_lock(['lock', 'platforms=linux', 'silent'], str(project))

    selected_new = push(selected_work, tmp_path / 'selected.git', {'new.txt': 'selected\n'}, 'selected update')
    push(other_work, tmp_path / 'other.git', {'new.txt': 'other\n'}, 'other update')
    run_lock(['lock', 'selected', 'platforms=linux', 'silent'], str(project))

    entries = by_name(project / 'mama.lock')
    assert entries['selected']['commit'] == selected_new
    assert entries['selected']['commit'] != selected_old
    assert entries['other']['commit'] == other_old


def test_targeted_refresh_preserves_other_platform_entries(tmp_path):
    linux_url, linux_work, _ = remote(tmp_path, 'linux-dep', dep_mamafile())
    windows_url, _, windows_head = remote(tmp_path, 'windows-dep', dep_mamafile())
    project = make_project(tmp_path, [platform_dep('linux', linux_url), platform_dep('windows', windows_url)])
    run_lock(['lock', 'platforms=linux,windows', 'silent'], str(project))

    linux_new = push(linux_work, tmp_path / 'linux-dep.git', {'new.txt': 'new\n'}, 'linux update')
    run_lock(['lock', 'linux-dep', 'platforms=linux', 'silent'], str(project))

    lock = read(project / 'mama.lock')
    assert lock['platforms'] == ['linux', 'windows']
    assert {name: entry['commit'] for name, entry in by_name(project / 'mama.lock').items()} == {
        'linux-dep': linux_new, 'windows-dep': windows_head}


def test_targeted_refresh_preserves_existing_transitive_dependency(tmp_path):
    child_url, child_work, child_old = remote(tmp_path, 'child', dep_mamafile())
    parent_url, _, parent_head = remote(tmp_path, 'parent', dep_mamafile([('child', child_url)]))
    project = make_project(tmp_path, [f"self.add_git('parent', '{parent_url}', git_branch='main')"])
    run_lock(['lock', 'platforms=linux', 'silent'], str(project))
    push(child_work, tmp_path / 'child.git', {'new.txt': 'new\n'}, 'advance child')

    run_lock(['lock', 'parent', 'platforms=linux', 'silent'], str(project))

    entries = by_name(project / 'mama.lock')
    assert entries['parent']['commit'] == parent_head
    assert entries['child']['commit'] == child_old


def test_targeted_refresh_rejects_unrelated_selector_drift(tmp_path):
    selected_url, _, _ = remote(tmp_path, 'selected', dep_mamafile())
    other_url, other_work, _ = remote(tmp_path, 'other', dep_mamafile())
    git(other_work, 'branch', 'maintenance')
    git(other_work, 'push', str(tmp_path / 'other.git'), 'maintenance')
    project = make_project(tmp_path, [f"self.add_git('selected', '{selected_url}', git_branch='main')",
                                      f"self.add_git('other', '{other_url}', git_branch='main')"])
    run_lock(['lock', 'platforms=linux', 'silent'], str(project))
    before = project.joinpath('mama.lock').read_text(encoding='utf-8')
    write_mamafile(project, [f"self.add_git('selected', '{selected_url}', git_branch='main')",
                             f"self.add_git('other', '{other_url}', git_branch='maintenance')"])

    with pytest.raises(RuntimeError, match='selector for other'):
        run_lock(['lock', 'selected', 'platforms=linux', 'silent'], str(project))

    assert project.joinpath('mama.lock').read_text(encoding='utf-8') == before


def test_full_refresh_advances_every_dependency(tmp_path):
    first_url, first_work, _ = remote(tmp_path, 'first', dep_mamafile())
    second_url, second_work, _ = remote(tmp_path, 'second', dep_mamafile())
    project = make_project(tmp_path, [f"self.add_git('first', '{first_url}', git_branch='main')",
                                      f"self.add_git('second', '{second_url}', git_branch='main')"])
    run_lock(['lock', 'platforms=linux', 'silent'], str(project))

    first_new = push(first_work, tmp_path / 'first.git', {'new.txt': 'first\n'}, 'first update')
    second_new = push(second_work, tmp_path / 'second.git', {'new.txt': 'second\n'}, 'second update')
    run_lock(['lock', 'platforms=linux', 'silent'], str(project))

    entries = by_name(project / 'mama.lock')
    assert entries['first']['commit'] == first_new
    assert entries['second']['commit'] == second_new


def test_exact_commit_refresh_loads_that_commits_transitive_graph(tmp_path):
    old_child_url, _, old_child_head = remote(tmp_path, 'old-child', dep_mamafile())
    new_child_url, _, _ = remote(tmp_path, 'new-child', dep_mamafile())
    parent_url, parent_work, parent_old = remote(tmp_path, 'parent', dep_mamafile([('old-child', old_child_url)]))
    parent_new = push(parent_work, tmp_path / 'parent.git',
                      {'mamafile.py': dep_mamafile([('new-child', new_child_url)])}, 'new dependency')
    project = make_project(tmp_path, [f"self.add_git('parent', '{parent_url}', git_branch='main')"])
    run_lock(['lock', 'platforms=linux', 'silent'], str(project))
    assert by_name(project / 'mama.lock')['parent']['commit'] == parent_new

    run_lock(['lock', 'parent', f'commit={parent_old[:10]}', 'platforms=linux', 'silent'], str(project))

    entries = by_name(project / 'mama.lock')
    assert entries['parent']['commit'] == parent_old
    assert entries['old-child']['commit'] == old_child_head
    assert 'new-child' not in entries


def test_unknown_exact_commit_does_not_replace_lock(tmp_path):
    url, _, _ = remote(tmp_path, 'dep', dep_mamafile())
    project = make_project(tmp_path, [f"self.add_git('dep', '{url}', git_branch='main')"])
    run_lock(['lock', 'platforms=linux', 'silent'], str(project))
    before = project.joinpath('mama.lock').read_text(encoding='utf-8')

    with pytest.raises(RuntimeError, match='unknown or ambiguous'):
        run_lock(['lock', 'dep', 'commit=deadbeef', 'platforms=linux', 'silent'], str(project))

    assert project.joinpath('mama.lock').read_text(encoding='utf-8') == before


@pytest.mark.parametrize('change', ['tracked', 'untracked'])
def test_lock_generation_rejects_dirty_checkout_without_replacing_lock(tmp_path, change):
    url, _, _ = remote(tmp_path, 'dep', dep_mamafile())
    project = make_project(tmp_path, [f"self.add_git('dep', '{url}', git_branch='main')"])
    run_lock(['lock', 'platforms=linux', 'silent'], str(project))
    before = project.joinpath('mama.lock').read_text(encoding='utf-8')
    checkout = project / 'packages' / 'dep' / 'dep'
    if change == 'tracked':
        mamafile = checkout / 'mamafile.py'
        mamafile.write_text(mamafile.read_text(encoding='utf-8') + '\n# local edit\n', encoding='utf-8')
    else:
        checkout.joinpath('untracked.cpp').write_text('// local edit\n', encoding='utf-8')

    with pytest.raises(RuntimeError, match='local modifications'):
        run_lock(['lock', 'platforms=linux', 'silent'], str(project))

    assert project.joinpath('mama.lock').read_text(encoding='utf-8') == before


def test_lock_generation_rejects_source_without_its_own_repository(tmp_path):
    url, _, _ = remote(tmp_path, 'dep', dep_mamafile())
    project = tmp_path / 'project'
    project.mkdir()
    git(project, 'init', '-q', '-b', 'main')
    declaration = root_mamafile([f"self.add_git('dep', '{url}', mamafile='mamadeps/dep.py', git_branch='main')"])
    commit(project, {'mamafile.py': declaration, 'mamadeps/dep.py': dep_mamafile(),
                     'packages/dep/dep/source.txt': 'source-only dependency\n'}, 'project source')

    with pytest.raises(RuntimeError, match='unusable Git repository'):
        run_lock(['lock', 'platforms=linux', 'silent'], str(project))

    assert not project.joinpath('mama.lock').exists()


def test_lock_generation_rejects_checkout_from_another_remote(tmp_path):
    declared_url, _, _ = remote(tmp_path, 'declared', dep_mamafile())
    executed = tmp_path / 'wrong-mamafile-executed'
    actual_mamafile = f"from pathlib import Path\nPath({str(executed)!r}).write_text('yes')\n" + dep_mamafile()
    actual_url, _, _ = remote(tmp_path, 'actual', actual_mamafile)
    project = make_project(tmp_path, [f"self.add_git('dep', '{declared_url}', git_branch='main')"])
    checkout = project / 'packages' / 'dep' / 'dep'
    checkout.parent.mkdir(parents=True)
    git(checkout.parent, 'clone', '-q', actual_url, str(checkout))

    with pytest.raises(RuntimeError, match='origin.*does not match'):
        run_lock(['lock', 'platforms=linux', 'silent'], str(project))

    assert not project.joinpath('mama.lock').exists()
    assert not executed.exists()


def test_lock_generation_rejects_an_offline_cached_branch(tmp_path):
    url, _, _ = remote(tmp_path, 'dep', dep_mamafile())
    project = make_project(tmp_path, [f"self.add_git('dep', '{url}', git_branch='main')"])
    run_lock(['lock', 'platforms=linux', 'silent'], str(project))
    before = project.joinpath('mama.lock').read_text(encoding='utf-8')
    remove_tree(tmp_path / 'dep.git')

    with pytest.raises(SystemExit):
        run_lock(['lock', 'platforms=linux', 'silent'], str(project))

    assert project.joinpath('mama.lock').read_text(encoding='utf-8') == before


def test_lock_generation_refreshes_a_stale_detached_branch(tmp_path):
    url, work, old = remote(tmp_path, 'dep', dep_mamafile())
    project = make_project(tmp_path, [f"self.add_git('dep', '{url}', git_branch='main')"])
    run_lock(['lock', 'platforms=linux', 'silent'], str(project))
    checkout = project / 'packages' / 'dep' / 'dep'
    build_dirs = [path for path in checkout.parent.iterdir() if path.is_dir() and path != checkout]
    assert len(build_dirs) == 1
    build_dirs[0].joinpath('git_status').write_text(Git.format_git_status(url, '', 'main', old[:7]), encoding='utf-8')
    new = push(work, tmp_path / 'dep.git', {'new.txt': 'new\n'}, 'advance branch')
    git(checkout, 'checkout', '-q', '--detach', old)

    run_lock(['lock', 'platforms=linux', 'silent'], str(project))

    assert by_name(project / 'mama.lock')['dep']['commit'] == new
    assert git(checkout, 'rev-parse', 'HEAD') == new


@pytest.mark.parametrize('status', ['missing', 'stale'])
def test_lock_generation_ignores_artifact_status(tmp_path, status):
    url, _, head = remote(tmp_path, 'dep', dep_mamafile())
    project = make_project(tmp_path, [f"self.add_git('dep', '{url}', git_branch='main')"])
    run_lock(['lock', 'platforms=linux', 'silent'], str(project))
    checkout = project / 'packages' / 'dep' / 'dep'
    build_dir = next(path for path in checkout.parent.iterdir() if path.is_dir() and path != checkout)
    if status == 'stale':
        build_dir.joinpath('git_status').write_text(
            Git.format_git_status('file:///old/dep.git', '', 'main', '0' * 7), encoding='utf-8')
    project.joinpath('mama.lock').unlink()

    with patch.object(Git, 'fetch_origin', autospec=True) as fetch, \
         patch.object(Git, 'reclone_wipe', autospec=True) as wipe, \
         patch.object(Git, 'clone_or_pull', autospec=True) as pull:
        run_lock(['lock', 'platforms=linux', 'silent'], str(project))

    fetch.assert_not_called()
    wipe.assert_not_called()
    pull.assert_not_called()
    assert by_name(project / 'mama.lock')['dep']['commit'] == head


def test_unreachable_exact_commit_does_not_replace_lock(tmp_path):
    url, work, _ = remote(tmp_path, 'dep', dep_mamafile())
    git(work, 'checkout', '-q', '-b', 'side')
    side = commit(work, {'side.txt': 'side\n'}, 'side commit')
    git(work, 'push', str(tmp_path / 'dep.git'), 'side')
    git(work, 'checkout', '-q', 'main')
    project = make_project(tmp_path, [f"self.add_git('dep', '{url}', git_branch='main')"])
    run_lock(['lock', 'platforms=linux', 'silent'], str(project))
    before = project.joinpath('mama.lock').read_text(encoding='utf-8')

    with pytest.raises(RuntimeError, match='not reachable'):
        run_lock(['lock', 'dep', f'commit={side[:10]}', 'platforms=linux', 'silent'], str(project))

    assert project.joinpath('mama.lock').read_text(encoding='utf-8') == before


def test_head_override_uses_the_current_remote_default_branch(tmp_path):
    url, work, _ = remote(tmp_path, 'dep', dep_mamafile())
    project = make_project(tmp_path, [f"self.add_git('dep', '{url}')"])
    run_lock(['lock', 'platforms=linux', 'silent'], str(project))
    checkout = project / 'packages' / 'dep' / 'dep'

    git(work, 'checkout', '-q', '-b', 'release')
    release = commit(work, {'release.txt': 'release\n'}, 'release head')
    git(work, 'push', str(tmp_path / 'dep.git'), 'release')
    git(tmp_path / 'dep.git', 'symbolic-ref', 'HEAD', 'refs/heads/release')
    assert git(checkout, 'symbolic-ref', 'refs/remotes/origin/HEAD') == 'refs/remotes/origin/main'

    run_lock(['lock', 'dep', f'commit={release[:10]}', 'platforms=linux', 'silent'], str(project))

    assert by_name(project / 'mama.lock')['dep']['commit'] == release
    assert git(checkout, 'rev-parse', 'HEAD') == release

    run_lock(['lock', 'platforms=linux', 'silent'], str(project))

    assert by_name(project / 'mama.lock')['dep']['commit'] == release
    assert git(checkout, 'rev-parse', 'HEAD') == release


@pytest.mark.parametrize('selector', ['tag', 'commit'])
def test_exact_commit_cannot_override_explicit_selector(tmp_path, selector):
    url, work, head = remote(tmp_path, 'dep', dep_mamafile())
    if selector == 'tag':
        git(work, 'tag', 'v1')
        git(work, 'push', str(tmp_path / 'dep.git'), 'v1')
        declaration = "git_tag='v1'"
    else:
        declaration = f"git_commit='{head}'"
    project = make_project(tmp_path, [f"self.add_git('dep', '{url}', {declaration})"])
    run_lock(['lock', 'platforms=linux', 'silent'], str(project))

    with pytest.raises(RuntimeError, match=f'cannot override dep declared with {selector}='):
        run_lock(['lock', 'dep', f'commit={head[:10]}', 'platforms=linux', 'silent'], str(project))


def test_build_lock_rejects_selector_drift(tmp_path):
    path = tmp_path / 'mama.lock'
    entry = LockEntry('dep', 'example.com/org/dep', LockSelector('branch', 'master'), 'a' * 40)
    lock = DependencyLock(str(path), ('linux',), {'dep': entry})
    git_source = Git('dep', 'git@example.com:org/dep.git', 'develop', '', None, True, [])

    with pytest.raises(RuntimeError, match='selector.*master.*develop'):
        lock.apply(git_source)


def test_build_lock_rejects_uncovered_platform(tmp_path):
    lock = DependencyLock(str(tmp_path / 'mama.lock'), ('linux',), {})
    with pytest.raises(RuntimeError, match='does not cover windows'):
        lock.validate_platform('windows')


def test_init_ignores_lock_platform_coverage(tmp_path):
    write_uncovered_lock(tmp_path)
    with patch('mama.main.mama_init_project') as initialize:
        mamabuild(['init', 'silent'], source_dir=str(tmp_path))
    initialize.assert_called_once()


def test_install_utility_ignores_lock_platform_coverage(tmp_path):
    write_uncovered_lock(tmp_path)
    with patch('mama.build_config.BuildConfig.run_convenient_installs') as install:
        mamabuild(['install-ndk-25c', 'silent'], source_dir=str(tmp_path))
    install.assert_called_once()


def test_lock_rejects_platform_alias(tmp_path):
    with pytest.raises(RuntimeError, match="msvc.*canonical name 'windows'"):
        run_lock(['lock', 'platforms=msvc', 'silent'], str(tmp_path))


def test_lock_reader_rejects_duplicate_dependencies(tmp_path):
    entry = lock_entry()
    tmp_path.joinpath('mama.lock').write_text(json.dumps(lock_data(dependencies=[entry, entry])), encoding='utf-8')

    with pytest.raises(RuntimeError, match='more than once'):
        read_lock(str(tmp_path))


def test_locked_build_drops_cached_shim_from_another_commit(tmp_path):
    dep = make_mock_shim_dep(tmp_path, build=True)
    dep.dep_source.locked_commit = 'def5678901234567890123456789012345678901'

    assert dep.try_load_cached_shim(check_staleness=False) is None
    assert not dep.is_artifactory_shim()


def test_locked_version_probe_reads_locked_commit_after_branch_advances(tmp_path):
    url, work, locked = remote(tmp_path, 'dep', "self.version = '1.0'\n")
    push(work, tmp_path / 'dep.git', {'mamafile.py': "self.version = '2.0'\n"}, 'advance version')
    git_source, dep = make_git_and_mock_dep(url=url, branch='main')
    git_source.locked_commit = locked
    dep.config.git_timeout = 30
    dep.config.is_network_available.return_value = True

    assert git_source.fetch_self_version_from_remote(dep) == '1.0'


def test_fresh_locked_clone_initializes_submodules_when_tip_matches(tmp_path):
    git_source, dep = make_git_and_mock_dep(unshallow=False)
    git_source.locked_commit = 'a' * 40
    dep.src_dir = str(tmp_path / 'dep')
    dep.config.is_network_available.return_value = True

    with patch.multiple(git_source, clone_with_filtered_progress=DEFAULT, checkout_locked_commit=DEFAULT,
                        update_submodules=DEFAULT) as mocks:
        mocks['checkout_locked_commit'].return_value = False
        git_source.clone_or_pull(dep)

    clone_args = mocks['clone_with_filtered_progress'].call_args.args[1]
    assert '--branch' not in clone_args
    mocks['update_submodules'].assert_called_once_with(dep, shallow=True)


@pytest.mark.parametrize('action', [('build',), ('update',), ('rebuild', 'dep')])
def test_normal_actions_keep_the_locked_commit(tmp_path, action):
    url, work, locked = remote(tmp_path, 'dep', dep_mamafile())
    project = make_project(tmp_path, [f"self.add_git('dep', '{url}', git_branch='main')"])
    run_lock(['lock', f'platforms={native_platform_name()}', 'silent'], str(project))
    push(work, tmp_path / 'dep.git', {'new.txt': 'new\n'}, 'advance branch')

    mamabuild([*action, native_platform_name(), 'noart', 'serial', 'silent'], source_dir=str(project))

    checkout = project / 'packages' / 'dep' / 'dep'
    assert git(checkout, 'rev-parse', 'HEAD') == locked


def test_list_does_not_consume_a_locked_commit_change(tmp_path):
    url, work, _ = remote(tmp_path, 'dep', BUILDING_DEP)
    old = push(work, tmp_path / 'dep.git', {'version.txt': 'A'}, 'version A')
    project = make_project(tmp_path, [f"self.add_git('dep', '{url}', git_branch='main')"])
    run_lock(['lock', f'platforms={native_platform_name()}', 'silent'], str(project))
    mamabuild(['build', native_platform_name(), 'noart', 'serial', 'silent'], source_dir=str(project))
    status = next(project.joinpath('packages').glob('*/*/git_status'))
    artifact = next(project.joinpath('packages').glob('*/*/libdep.a'))

    new = push(work, tmp_path / 'dep.git', {'version.txt': 'B'}, 'advance branch')
    lock = read(project / 'mama.lock')
    lock['dependencies'][0]['commit'] = new
    project.joinpath('mama.lock').write_text(json.dumps(lock), encoding='utf-8')

    mamabuild(['list', native_platform_name(), 'noart', 'serial', 'silent'], source_dir=str(project))
    assert git(project / 'packages' / 'dep' / 'dep', 'rev-parse', 'HEAD') == new
    assert status.read_text(encoding='utf-8').splitlines()[3] == old
    assert artifact.read_text(encoding='utf-8') == 'A'
    mamabuild(['build', native_platform_name(), 'noart', 'serial', 'silent'], source_dir=str(project))
    assert status.read_text(encoding='utf-8').splitlines()[3] == new
    assert artifact.read_text(encoding='utf-8') == 'B'


def test_failed_locked_build_does_not_consume_commit_change(tmp_path):
    url, work, _ = remote(tmp_path, 'dep', BUILDING_DEP)
    old = push(work, tmp_path / 'dep.git', {'version.txt': 'A'}, 'version A')
    project = make_project(tmp_path, [f"self.add_git('dep', '{url}', git_branch='main')"])
    run_lock(['lock', f'platforms={native_platform_name()}', 'silent'], str(project))
    mamabuild(['build', native_platform_name(), 'noart', 'serial', 'silent'], source_dir=str(project))
    status = next(project.joinpath('packages').glob('*/*/git_status'))
    artifact = next(project.joinpath('packages').glob('*/*/libdep.a'))

    new = push(work, tmp_path / 'dep.git', {'version.txt': 'B', 'fail-build': ''}, 'failing version B')
    lock = read(project / 'mama.lock')
    lock['dependencies'][0]['commit'] = new
    project.joinpath('mama.lock').write_text(json.dumps(lock), encoding='utf-8')

    for _ in range(2):
        with pytest.raises(SystemExit):
            mamabuild(['build', native_platform_name(), 'noart', 'serial', 'silent'], source_dir=str(project))
        assert status.read_text(encoding='utf-8').splitlines()[3] == old
        assert artifact.read_text(encoding='utf-8') == 'A'


def test_failed_locked_package_does_not_consume_commit_change(tmp_path):
    url, work, _ = remote(tmp_path, 'dep', BUILDING_DEP)
    old = push(work, tmp_path / 'dep.git', {'version.txt': 'A'}, 'version A')
    project = make_project(tmp_path, [f"self.add_git('dep', '{url}', git_branch='main')"])
    run_lock(['lock', f'platforms={native_platform_name()}', 'silent'], str(project))
    mamabuild(['build', native_platform_name(), 'noart', 'serial', 'silent'], source_dir=str(project))
    status = next(project.joinpath('packages').glob('*/*/git_status'))
    artifact = next(project.joinpath('packages').glob('*/*/libdep.a'))

    new = push(work, tmp_path / 'dep.git', {'version.txt': 'B', 'fail-package-once': ''},
               'version B with one failed package')
    lock = read(project / 'mama.lock')
    lock['dependencies'][0]['commit'] = new
    project.joinpath('mama.lock').write_text(json.dumps(lock), encoding='utf-8')

    with pytest.raises(SystemExit):
        mamabuild(['build', native_platform_name(), 'noart', 'serial', 'silent'], source_dir=str(project))
    assert status.read_text(encoding='utf-8').splitlines()[3] == old
    assert artifact.read_text(encoding='utf-8') == 'BROKEN'

    mamabuild(['build', native_platform_name(), 'noart', 'serial', 'silent'], source_dir=str(project))
    assert status.read_text(encoding='utf-8').splitlines()[3] == new
    assert artifact.read_text(encoding='utf-8') == 'B'


def test_targeted_build_rebuilds_a_stale_locked_child(tmp_path):
    child_recipe = BUILDING_DEP.replace('class dep(', 'class child(')
    child_url, child_work, _ = remote(tmp_path, 'child', child_recipe)
    push(child_work, tmp_path / 'child.git', {'version.txt': 'A'}, 'version A')
    parent_recipe = dep_mamafile([('child', child_url)]).replace('class dependency(', 'class parent(')
    parent_url, _, _ = remote(tmp_path, 'parent', parent_recipe)
    project = make_project(tmp_path, [f"self.add_git('parent', '{parent_url}', git_branch='main')"])
    run_lock(['lock', f'platforms={native_platform_name()}', 'silent'], str(project))
    mamabuild(['build', native_platform_name(), 'noart', 'serial', 'silent'], source_dir=str(project))
    artifact = next(project.joinpath('packages').glob('*/*/libdep.a'))
    assert artifact.read_text(encoding='utf-8') == 'A'

    new = push(child_work, tmp_path / 'child.git', {'version.txt': 'B'}, 'version B')
    lock = read(project / 'mama.lock')
    next(entry for entry in lock['dependencies'] if entry['name'] == 'child')['commit'] = new
    project.joinpath('mama.lock').write_text(json.dumps(lock), encoding='utf-8')

    mamabuild(['build', 'parent', native_platform_name(), 'noart', 'serial', 'silent'], source_dir=str(project))
    assert artifact.read_text(encoding='utf-8') == 'B'


@pytest.mark.parametrize('action', ['deploy', 'upload', 'test', 'start'])
def test_non_build_consumer_rejects_stale_locked_artifacts(tmp_path, action, capsys):
    url, work, _ = remote(tmp_path, 'dep', BUILDING_DEP)
    old = push(work, tmp_path / 'dep.git', {'version.txt': 'A'}, 'version A')
    project = make_project(tmp_path, [f"self.add_git('dep', '{url}', git_branch='main')"])
    run_lock(['lock', f'platforms={native_platform_name()}', 'silent'], str(project))
    mamabuild(['build', native_platform_name(), 'noart', 'serial', 'silent'], source_dir=str(project))
    status = next(project.joinpath('packages').glob('*/*/git_status'))
    artifact = next(project.joinpath('packages').glob('*/*/libdep.a'))

    new = push(work, tmp_path / 'dep.git', {'version.txt': 'B'}, 'version B')
    lock = read(project / 'mama.lock')
    lock['dependencies'][0]['commit'] = new
    project.joinpath('mama.lock').write_text(json.dumps(lock), encoding='utf-8')

    with pytest.raises(SystemExit):
        mamabuild([action, 'dep', native_platform_name(), 'noart', 'silent'], source_dir=str(project))

    assert 'stale artifacts' in capsys.readouterr().out
    assert git(project / 'packages' / 'dep' / 'dep', 'rev-parse', 'HEAD') == new
    assert status.read_text(encoding='utf-8').splitlines()[3] == old
    assert artifact.read_text(encoding='utf-8') == 'A'
    assert not artifact.parent.joinpath('deploy').exists()


def test_plain_build_validates_the_default_host_platform(tmp_path):
    url, _, locked = remote(tmp_path, 'dep', dep_mamafile())
    project = make_project(tmp_path, [f"self.add_git('dep', '{url}', git_branch='main')"])
    run_lock(['lock', f'platforms={native_platform_name()}', 'silent'], str(project))

    mamabuild(['build', 'noart', 'serial', 'silent'], source_dir=str(project))

    checkout = project / 'packages' / 'dep' / 'dep'
    assert git(checkout, 'rev-parse', 'HEAD') == locked


def test_clean_shallow_clone_falls_back_when_direct_commit_fetch_is_rejected(tmp_path):
    url, work, locked = remote(tmp_path, 'dep', dep_mamafile())
    project = make_project(tmp_path, [f"self.add_git('dep', '{url}', git_branch='main')"])
    run_lock(['lock', f'platforms={native_platform_name()}', 'silent'], str(project))
    remove_tree(project / 'packages')
    push(work, tmp_path / 'dep.git', {'new.txt': 'new\n'}, 'advance branch')
    real_run_git = Git.run_git

    def reject_direct_fetch(self, dep, command, throw=True):
        if command == f'fetch --depth 1 origin {locked}': return 1
        return real_run_git(self, dep, command, throw)

    with patch.object(Git, 'run_git', new=reject_direct_fetch):
        mamabuild(['build', native_platform_name(), 'noart', 'serial', 'silent'], source_dir=str(project))

    checkout = project / 'packages' / 'dep' / 'dep'
    assert git(checkout, 'rev-parse', 'HEAD') == locked
