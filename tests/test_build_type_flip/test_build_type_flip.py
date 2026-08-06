"""Pins the build-type flip: debug and release share a build dir, so only a reconfigure switches it,
and a mixed tree says so."""
import os
from types import SimpleNamespace
from unittest.mock import patch
import pytest

from mama.utils.fileio import write_text_to
from mama.utils.paths import path_join
from mama.papa_deploy import PapaFileInfo

from testutils import (make_archive_name_target, make_configured_target, write_cmake_cache,
                       write_build_file, run_config_capturing)
from mama import artifactory as art, build_names
from mama.build_config import DeployStats
import mama.dependency_chain as dc
from mama.buildsys.cmake import configure as cc

RELEASE = 'CMAKE_GENERATOR:INTERNAL=Ninja\nCMAKE_BUILD_TYPE:STRING=RelWithDebInfo\n'
DEBUG = 'CMAKE_GENERATOR:INTERNAL=Ninja\nCMAKE_BUILD_TYPE:STRING=Debug\n'


def _configured(tmp_path, cache, **cfg):
    t, dep = make_configured_target(tmp_path, **cfg)
    write_cmake_cache(t.build_dir(), cache)
    write_build_file(t.build_dir())
    return t, dep


def _configures(t, dep) -> bool:
    """True when run_config actually ran a cmake configure."""
    return bool(run_config_capturing(t, dep))


def test_a_debug_run_over_a_release_dir_reconfigures(tmp_path):
    # the two share a build dir, and a single-config generator bakes the type into the cache
    assert _configures(*_configured(tmp_path, RELEASE, debug=True))


def test_a_release_run_over_a_debug_dir_reconfigures(tmp_path):
    assert _configures(*_configured(tmp_path, DEBUG, debug=False))


@pytest.mark.parametrize('cache,debug', [(RELEASE, False), (DEBUG, True)])
def test_a_matching_build_type_still_skips_the_reconfigure(tmp_path, cache, debug):
    assert not _configures(*_configured(tmp_path, cache, debug=debug))


def test_a_dir_with_no_cache_entry_does_not_force_a_reconfigure(tmp_path):
    # a pre-fingerprint dir records no type, and guessing would reconfigure every package once
    assert not _configures(*_configured(tmp_path, 'CMAKE_GENERATOR:INTERNAL=Ninja\n', debug=True))


def test_cached_build_type_reads_the_cache_and_tolerates_a_missing_dir(tmp_path):
    assert cc.cached_build_type(str(tmp_path / 'nope')) == ''
    write_cmake_cache(str(tmp_path), DEBUG)
    assert cc.cached_build_type(str(tmp_path)) == 'Debug'


# --- the mixed-tree warning -------------------------------------------------

def _dep(tmp_path, name, cache=None):
    build_dir = tmp_path / name
    if cache: write_cmake_cache(str(build_dir), cache)
    cfg = SimpleNamespace(print=True, debug=False, deploy_stats=DeployStats())
    return SimpleNamespace(name=name, config=cfg, build_dir=str(build_dir),
                           should_rebuild=False, from_artifactory=False, nothing_to_build=True)


def test_a_mixed_tree_names_every_package_of_the_other_build_type(tmp_path):
    deps = [_dep(tmp_path, 'a', RELEASE), _dep(tmp_path, 'b', DEBUG), _dep(tmp_path, 'c', DEBUG)]
    with patch('mama.dependency_chain.warning') as warn:
        dc._print_build_summary(deps, 1.0)
    said = ' '.join(c[0][0] for c in warn.call_args_list)
    assert 'debug' in said and 'b' in said and 'c' in said
    assert ' a ' not in said  # the package that matches this run is not worth naming


def test_a_fetched_package_names_its_build_type_from_papa_txt(tmp_path):
    # A shim and a fetched package hold no cmake cache, so only the papa `O` record says what they are.
    dep = _dep(tmp_path, 'fetched')
    os.makedirs(dep.build_dir, exist_ok=True)
    write_text_to(path_join(dep.build_dir, 'papa.txt'), 'P fetched\nO debug linux x64\nL lib/libfetched.a\n')
    assert dc._dep_build_type(dep) == 'debug'


def test_a_uniform_tree_says_nothing(tmp_path):
    deps = [_dep(tmp_path, 'a', RELEASE), _dep(tmp_path, 'b', RELEASE)]
    with patch('mama.dependency_chain.warning') as warn:
        dc._print_build_summary(deps, 1.0)
    warn.assert_not_called()


# --- the flip never cascades ------------------------------------------------

def test_a_dep_that_is_not_building_keeps_its_own_build_type(tmp_path):
    # mixing is the point: one target goes debug for gdb, and its deps stay release. The check lives
    # inside run_config, which only a target with real build work ever reaches.
    t, dep = _configured(tmp_path, RELEASE, debug=True)
    dep.should_rebuild = False
    dep.from_artifactory = False
    dep.nothing_to_build = False
    with patch.object(type(t), '_cmake_configure_step') as step:
        t.configure_phase()
    step.assert_not_called()
    assert cc.cached_build_type(t.build_dir()) == 'RelWithDebInfo'


def test_the_target_that_is_building_does_reach_the_check(tmp_path):
    t, dep = _configured(tmp_path, RELEASE, debug=True)
    dep.should_rebuild = True
    dep.from_artifactory = False
    dep.nothing_to_build = False
    with patch.object(type(t), '_cmake_configure_step') as step:
        t.configure_phase()
    step.assert_called_once()


# --- the archive name follows the artifacts ---------------------------------

def _named(tmp_path, cache, release, from_build_dir):
    """The archive name the upload composes (from_build_dir) or the one a download asks for."""
    if cache: write_cmake_cache(str(tmp_path), cache)
    target = make_archive_name_target(version='1.0', release=release, build_dir=str(tmp_path))
    built = build_names.build_dir_build_type(target.dep) if from_build_dir else ''
    return art.artifactory_archive_name(target, build_type=built)


@pytest.mark.parametrize('cache,release,expect', [
    (DEBUG,   True,  'debug'),     # the run says release, the dir holds debug: the archive is debug
    (RELEASE, False, 'release'),
    (None,    True,  'release'),   # no cache to read, so the run decides
])
def test_an_upload_names_the_build_type_the_dir_holds(tmp_path, cache, release, expect):
    assert _named(tmp_path, cache, release, from_build_dir=True) == f'pkg-linux-24-gcc14-x64-{expect}-1.0'


def test_a_multi_config_dir_names_the_build_type_the_run_asks_for(tmp_path):
    # Visual Studio and Xcode keep both types in one dir and pick at build time, so the deploy follows
    # the run. Only a single-config dir proves what its artifacts are.
    vs = 'CMAKE_GENERATOR:INTERNAL=Visual Studio 17 2022\nCMAKE_BUILD_TYPE:STRING=Debug\n'
    assert _named(tmp_path, vs, True, from_build_dir=True) == 'pkg-linux-24-gcc14-x64-release-1.0'


def test_a_download_names_the_build_type_the_run_asks_for(tmp_path):
    # The name picks the package to fetch, so a stale dir must never redirect it.
    assert _named(tmp_path, DEBUG, True, from_build_dir=False) == 'pkg-linux-24-gcc14-x64-release-1.0'


# --- the papa `O` record ----------------------------------------------------

def test_the_object_record_names_every_axis_of_the_artifacts(tmp_path):
    write_cmake_cache(str(tmp_path), DEBUG)
    target = make_archive_name_target(release=True, build_dir=str(tmp_path), args=['LGPL'], sanitize='address')
    assert build_names.object_attributes(target) == 'debug linux x64 asan lgpl'


@pytest.mark.parametrize('text,expect', [
    ('P libfoo\nC gcc13.3\nO debug linux x64 asan\nL lib/libfoo.a\n', ['debug', 'linux', 'x64', 'asan']),
    ('P libfoo\nL lib/libfoo.a\n', []),   # a package written before the O record
])
def test_a_papa_file_reads_the_object_record(tmp_path, text, expect):
    papa = tmp_path / 'papa.txt'
    papa.write_text(text)
    assert PapaFileInfo(str(papa)).attributes == expect
