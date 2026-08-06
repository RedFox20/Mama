"""Pins the build-type flip: debug and release share a build dir, so only a reconfigure switches it,
and a mixed tree says so."""
from types import SimpleNamespace
from unittest.mock import patch
import pytest

from testutils import make_configured_target, write_cmake_cache, write_build_file, run_config_capturing
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
    cfg = SimpleNamespace(print=True, debug=False)
    return SimpleNamespace(name=name, config=cfg, build_dir=str(build_dir),
                           should_rebuild=False, from_artifactory=False, nothing_to_build=True)


def test_a_mixed_tree_names_every_package_of_the_other_build_type(tmp_path):
    deps = [_dep(tmp_path, 'a', RELEASE), _dep(tmp_path, 'b', DEBUG), _dep(tmp_path, 'c', DEBUG)]
    with patch('mama.dependency_chain.warning') as warn:
        dc._print_build_summary(deps, 1.0)
    said = ' '.join(c[0][0] for c in warn.call_args_list)
    assert 'RelWithDebInfo' in said and 'b' in said and 'c' in said
    assert ' a ' not in said  # the package that matches this run is not worth naming


def test_a_uniform_tree_says_nothing(tmp_path):
    deps = [_dep(tmp_path, 'a', RELEASE), _dep(tmp_path, 'b', RELEASE)]
    with patch('mama.dependency_chain.warning') as warn:
        dc._print_build_summary(deps, 1.0)
    warn.assert_not_called()
