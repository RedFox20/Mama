"""Pins that a target fetched from artifactory still runs package(), so a deploy keeps the export rules."""
import itertools
from unittest.mock import patch
import pytest

from testutils import make_package_target

from mama.build_target import BuildTarget
from mama.utils.errors import BuildError

CATEGORIES = ('includes', 'libs', 'syslibs', 'assets')
ARCHIVE = {'includes': ['/pkg/include'], 'libs': ['/pkg/lib/libfoo.a'],
           'syslibs': ['dl'], 'assets': ['/pkg/bin/tool']}
HOOK = {'includes': ['/hook/include'], 'libs': ['/hook/lib/libhook.a'],
        'syslibs': ['hooksys'], 'assets': ['/hook/bin/hooktool']}
INC_FILTER = ['.h', '.inc']


def _hook_that_declares(*categories):
    """A package() that exports exactly `categories`, appending the way export_*() does."""
    def package(self):
        for c in categories:
            getattr(self, f'exported_{c}').extend(HOOK[c])
    return package


def _target(tmp_path, package_hook, *, fetched=True, rebuild=False, **config):
    """A target whose papa.txt exports are already loaded, the way artifactory_load_target leaves it."""
    archive = tuple(list(ARCHIVE[c]) for c in CATEGORIES) if fetched else None
    return make_package_target(tmp_path, package=package_hook, exports=archive, **config,
                               dep_attrs={'from_artifactory': fetched, 'should_rebuild': rebuild,
                                          'has_usable_artifacts': lambda: True,
                                          'artifactory_archive': 'foo-ubuntu-24-x64-release-abc1234'})


def _exports_by_category(target):
    return {c: getattr(target, f'exported_{c}') for c in CATEGORIES}


def _every_subset():
    for size in range(len(CATEGORIES) + 1):
        yield from itertools.combinations(CATEGORIES, size)


# --- the export matrix ------------------------------------------------------

@pytest.mark.parametrize('declared', _every_subset(), ids=lambda d: '_'.join(d) or 'nothing')
def test_a_fetched_target_keeps_the_archive_list_for_every_category_the_hook_skips(tmp_path, declared):
    target = _target(tmp_path, _hook_that_declares(*declared))
    with patch.object(BuildTarget, 'default_package_includes') as inc, \
         patch.object(BuildTarget, 'default_package_libs') as libs:
        target._run_packaging()
    assert _exports_by_category(target) == {c: HOOK[c] if c in declared else ARCHIVE[c] for c in CATEGORIES}
    inc.assert_not_called(); libs.assert_not_called()  # the archive list is the truth, never a glob


@pytest.mark.parametrize('declared', _every_subset(), ids=lambda d: '_'.join(d) or 'nothing')
def test_a_source_target_falls_back_to_the_default_packaging(tmp_path, declared):
    target = _target(tmp_path, _hook_that_declares(*declared), fetched=False, rebuild=True)
    with patch.object(BuildTarget, 'default_package_includes') as inc, \
         patch.object(BuildTarget, 'default_package_libs') as libs:
        target._run_packaging()
    assert inc.called is ('includes' not in declared)
    assert libs.called is not ('libs' in declared or 'syslibs' in declared)
    for c in declared:
        assert getattr(target, f'exported_{c}') == HOOK[c]


@pytest.mark.parametrize('fetched', [True, False])
def test_no_export_includes_stops_the_default_include_packaging(tmp_path, fetched):
    def package(self): self.no_export_includes()
    target = _target(tmp_path, package, fetched=fetched, rebuild=True)
    with patch.object(BuildTarget, 'default_package_includes') as inc:
        target._run_packaging()
    inc.assert_not_called()


@pytest.mark.parametrize('fetched', [True, False])
def test_no_export_libs_stops_the_default_lib_packaging(tmp_path, fetched):
    def package(self): self.no_export_libs()
    target = _target(tmp_path, package, fetched=fetched, rebuild=True)
    with patch.object(BuildTarget, 'default_package_libs') as libs:
        target._run_packaging()
    libs.assert_not_called()


# --- the export RULES, which papa.txt never records -------------------------

def _declares_inc(self):
    self.export_include('include', build_dir=True, includes_filter=INC_FILTER)


def test_a_fetched_target_runs_package_so_the_include_filter_survives(tmp_path):
    # the deploy reads include_glob_filter, so a skipped hook ships the default suffixes only
    target = _target(tmp_path, _declares_inc)
    with patch.object(BuildTarget, 'export_include', autospec=True) as export:
        target._run_packaging()
    assert export.call_args.kwargs['includes_filter'] == INC_FILTER


# --- recovery ---------------------------------------------------------------

def test_a_failing_package_keeps_the_fetched_package_usable(tmp_path):
    def boom(self): raise RuntimeError('libfoo.so not found at /pkg/lib')
    target = _target(tmp_path, boom, print=True)
    with patch('mama.build_target.warning') as warn:
        target._run_packaging()  # must not raise: the archive is still usable
    assert 'INCOMPLETE' in warn.call_args[0][0]
    assert _exports_by_category(target) == ARCHIVE


def test_a_package_that_fails_halfway_keeps_the_archive_list_for_the_rest(tmp_path):
    def half(self):
        self.exported_includes.extend(HOOK['includes'])
        raise RuntimeError('the lib glob found nothing')
    target = _target(tmp_path, half, print=True)
    with patch('mama.build_target.warning'):
        target._run_packaging()
    assert target.exported_includes == HOOK['includes']  # what it managed to declare
    assert target.exported_libs == ARCHIVE['libs']       # and the archive covers the rest


def test_a_source_target_still_fails_loudly_on_a_bad_package(tmp_path):
    def boom(self): raise RuntimeError('libfoo.so not found at /pkg/lib')
    target = _target(tmp_path, boom, fetched=False, rebuild=True, list=False)
    with pytest.raises(BuildError, match='Package failed for target libfoo'):
        target._run_packaging()


def test_a_list_run_reports_a_bad_package_and_carries_on(tmp_path):
    def boom(self): raise RuntimeError('libfoo.so not found at /pkg/lib')
    target = _target(tmp_path, boom, fetched=False, rebuild=True, list=True, print=True)
    with patch('mama.build_target.warning') as warn:
        target._run_packaging()
    assert 'INCOMPLETE' in warn.call_args[0][0]


# --- the listing and the papa file ------------------------------------------

@pytest.mark.parametrize('rebuild,expected', [(False, 'artifactory-cache foo-ubuntu-24-x64-release-abc1234'),
                                              (True, 'target.package()')])
def test_the_listing_names_where_the_exports_came_from(tmp_path, rebuild, expected):
    target = _target(tmp_path, _hook_that_declares('includes'), rebuild=rebuild)
    target._run_packaging()
    assert target.packaging_result == expected


def test_a_plain_build_keeps_the_papa_file_its_shim_cache_needs(tmp_path):
    # deleting papa.txt here would make the next run miss the shim cache and re-fetch the archive
    target = _target(tmp_path, _hook_that_declares('includes'), print=True)
    papa = tmp_path / 'packages/libfoo/linux/papa.txt'
    papa.parent.mkdir(parents=True, exist_ok=True); papa.write_text('P libfoo\n')
    target._run_packaging()
    assert papa.exists()


def test_a_rebuild_drops_a_papa_file_whose_exports_no_longer_match(tmp_path):
    target = _target(tmp_path, _hook_that_declares('includes'), rebuild=True, print=True)
    papa = tmp_path / 'packages/libfoo/linux/papa.txt'
    papa.parent.mkdir(parents=True, exist_ok=True); papa.write_text('P libfoo\n')
    target._run_packaging()
    assert not papa.exists()
