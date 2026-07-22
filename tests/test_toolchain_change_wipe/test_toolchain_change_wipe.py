"""Pins auto-heal of a build dir whose toolchain moved since it was configured (compiler/NDK/SDK path change):
the dir is wiped and reconfigured, while an unchanged toolchain is adopted, never wiped."""
import os, pytest
from unittest.mock import patch

from testutils import make_configured_target, write_cmake_cache, write_build_file
from mama import cmake_configure as cc

NINJA = 'CMAKE_GENERATOR:INTERNAL=Ninja\n'


def _valid_cache(build_dir, cxx='', c=''):
    """A complete Ninja cache (+ build.ninja) so is_cmake_cache_valid() passes, optionally recording the
    compiler paths the unfingerprinted-heal path compares against."""
    text = NINJA
    if cxx: text += f'CMAKE_CXX_COMPILER:FILEPATH={cxx}\n'
    if c:   text += f'CMAKE_C_COMPILER:FILEPATH={c}\n'
    write_cmake_cache(build_dir, text); write_build_file(build_dir)


def _write_fp(build_dir, fp):
    with open(os.path.join(build_dir, cc._TOOLCHAIN_FINGERPRINT_FILE), 'w', encoding='utf-8') as f: f.write(fp)


def _read_fp(build_dir):
    p = os.path.join(build_dir, cc._TOOLCHAIN_FINGERPRINT_FILE)
    return open(p, encoding='utf-8').read().strip() if os.path.exists(p) else ''


def _run(t, dep, fingerprint='FP', configure_raises=False):
    """run_config with the cmake call + seed coordinator stubbed and the fingerprint pinned; returns the
    recorded conf calls (['conf'] => reconfigured, [] => skipped)."""
    calls = []
    def conf(*a, **k):
        calls.append('conf')
        if configure_raises: raise RuntimeError('cmake exploded')
    with patch('mama.cmake_configure._rerunnable_cmake_conf', side_effect=conf), \
         patch('mama.cmake_configure.compute_env', return_value={}), \
         patch('mama.cmake_configure._seed_coordinator') as coord, \
         patch('mama.cmake_configure._toolchain_fingerprint', return_value=fingerprint), \
         patch.object(dep, 'get_enabled_sanitizers', return_value=''):
        coord.return_value.prepare.return_value = 'none'
        coord.return_value.status.return_value = (fingerprint, False)
        cc.run_config(t)
    return calls


# ── _cache_entry (CMakeCache line parser) ────────────────────────────────────

def test_cache_entry_parses_typed_untyped_and_missing():
    text = 'CMAKE_CXX_COMPILER:FILEPATH=/opt/clang++\nBARE=value\nCMAKE_C_COMPILER=/no/type\n'
    assert cc._cache_entry(text, 'CMAKE_CXX_COMPILER') == '/opt/clang++'  # KEY:TYPE=value
    assert cc._cache_entry(text, 'CMAKE_C_COMPILER') == '/no/type'        # KEY=value (type omitted)
    assert cc._cache_entry(text, 'BARE') == 'value'
    assert cc._cache_entry(text, 'CMAKE_ABSENT') == ''


# ── _toolchain_moved_unfingerprinted (bootstrap heal for pre-fingerprint dirs) ─

def test_moved_unfingerprinted_true_when_compiler_path_changed(tmp_path):
    t, dep = make_configured_target(tmp_path, compiler=('/opt/ndk-B/clang', '/opt/ndk-B/clang++', '21'))
    _valid_cache(t.build_dir(), cxx='/opt/ndk-A/clang++', c='/opt/ndk-A/clang')
    assert cc._toolchain_moved_unfingerprinted(t.build_dir(), t) is True


def test_moved_unfingerprinted_false_when_compiler_unchanged_and_present(tmp_path):
    cxx = tmp_path / 'clang++'; cxx.write_text('#!/bin/sh\n')   # a compiler still on disk
    c = tmp_path / 'clang'; c.write_text('#!/bin/sh\n')
    t, dep = make_configured_target(tmp_path, compiler=(str(c), str(cxx), '21'))
    _valid_cache(t.build_dir(), cxx=str(cxx), c=str(c))
    assert cc._toolchain_moved_unfingerprinted(t.build_dir(), t) is False


def test_moved_unfingerprinted_true_when_recorded_compiler_vanished(tmp_path):
    gone = str(tmp_path / 'ndk' / 'clang++')   # same path both sides, but the binary is gone (store GC'd)
    t, dep = make_configured_target(tmp_path, compiler=(gone, gone, '21'))
    _valid_cache(t.build_dir(), cxx=gone)
    assert cc._toolchain_moved_unfingerprinted(t.build_dir(), t) is True


def test_moved_unfingerprinted_false_without_a_cached_compiler(tmp_path):
    t, dep = make_configured_target(tmp_path)
    _valid_cache(t.build_dir())   # generator only, no compiler lines
    assert cc._toolchain_moved_unfingerprinted(t.build_dir(), t) is False


def test_moved_unfingerprinted_false_when_no_explicit_compiler(tmp_path):
    # MSVC/unresolved: mama writes no CMAKE_*_COMPILER define, so there is nothing to compare against
    t, dep = make_configured_target(tmp_path, compiler=('', '', ''))
    _valid_cache(t.build_dir(), cxx='/whatever/cl.exe')
    assert cc._toolchain_moved_unfingerprinted(t.build_dir(), t) is False


# ── run_config: recorded-fingerprint path ────────────────────────────────────

def test_changed_fingerprint_wipes_and_reconfigures(tmp_path):
    t, dep = make_configured_target(tmp_path)
    _valid_cache(t.build_dir()); _write_fp(t.build_dir(), 'OLD')
    assert _run(t, dep, fingerprint='NEW') == ['conf']
    assert not os.path.exists(t.build_dir('CMakeCache.txt'))
    assert _read_fp(t.build_dir()) == 'NEW'


def test_matching_fingerprint_skips_without_touching_the_dir(tmp_path):
    t, dep = make_configured_target(tmp_path)
    _valid_cache(t.build_dir()); _write_fp(t.build_dir(), 'SAME')
    assert _run(t, dep, fingerprint='SAME') == []
    assert os.path.exists(t.build_dir('CMakeCache.txt'))
    assert _read_fp(t.build_dir()) == 'SAME'


# ── run_config: unfingerprinted (pre-feature) dirs ───────────────────────────

def test_unfingerprinted_moved_compiler_heals_once(tmp_path):
    t, dep = make_configured_target(tmp_path, compiler=('/opt/ndk-B/clang', '/opt/ndk-B/clang++', '21'))
    _valid_cache(t.build_dir(), cxx='/opt/ndk-A/clang++', c='/opt/ndk-A/clang')   # NO fingerprint file
    assert _run(t, dep, fingerprint='NEW') == ['conf']
    assert not os.path.exists(t.build_dir('CMakeCache.txt'))
    assert _read_fp(t.build_dir()) == 'NEW'   # now fingerprinted, so future checks are exact


def test_unfingerprinted_unchanged_compiler_is_adopted_not_wiped(tmp_path):
    # THE anti-mass-invalidation guarantee: a warm dir with an unchanged toolchain and no fingerprint yet is
    # adopted (fingerprint recorded), never wiped.
    cxx = tmp_path / 'clang++'; cxx.write_text('#!/bin/sh\n')
    t, dep = make_configured_target(tmp_path, compiler=(str(cxx), str(cxx), '21'))
    _valid_cache(t.build_dir(), cxx=str(cxx))
    assert _run(t, dep, fingerprint='FP') == []
    assert os.path.exists(t.build_dir('CMakeCache.txt'))
    assert _read_fp(t.build_dir()) == 'FP'


# ── run_config: fresh dir + one-shot + flip-flop + failure ───────────────────

def test_fresh_dir_configures_and_records_fingerprint(tmp_path):
    t, dep = make_configured_target(tmp_path)   # no cache at all
    assert _run(t, dep, fingerprint='FP') == ['conf']
    assert _read_fp(t.build_dir()) == 'FP'


def test_repeated_identical_runs_do_not_flipflop(tmp_path):
    # The core no-flip-flop guarantee: same toolchain, same valid dir -> skip every time, never a wipe.
    t, dep = make_configured_target(tmp_path)
    _valid_cache(t.build_dir()); _write_fp(t.build_dir(), 'FP')
    for _ in range(3):
        assert _run(t, dep, fingerprint='FP') == []
        assert os.path.exists(t.build_dir('CMakeCache.txt'))
        assert _read_fp(t.build_dir()) == 'FP'


def test_wipe_is_one_shot_not_repeating(tmp_path):
    t, dep = make_configured_target(tmp_path)
    _valid_cache(t.build_dir()); _write_fp(t.build_dir(), 'OLD')
    assert _run(t, dep, fingerprint='NEW') == ['conf']
    _valid_cache(t.build_dir())                          # emulate the reconfigure the stub didn't rerun
    assert _run(t, dep, fingerprint='NEW') == []         # fingerprint now matches -> no second wipe
    assert os.path.exists(t.build_dir('CMakeCache.txt'))


def test_failed_configure_records_no_fingerprint_baseline(tmp_path):
    t, dep = make_configured_target(tmp_path)   # fresh dir, role='none' -> the exception propagates
    with pytest.raises(RuntimeError):
        _run(t, dep, fingerprint='FP', configure_raises=True)
    assert _read_fp(t.build_dir()) == ''        # a broken configure must not leave a false baseline


# ── the wrapper itself is a pure hash of _seed_inputs (no wrapper-level drift) ─

def test_toolchain_fingerprint_is_a_pure_hash_of_seed_inputs():
    with patch('mama.cmake_configure._seed_inputs', return_value={'cc': {'path': '/a'}}):
        a = cc._toolchain_fingerprint(object())
        b = cc._toolchain_fingerprint(object())   # same inputs -> stable across runs
    with patch('mama.cmake_configure._seed_inputs', return_value={'cc': {'path': '/b'}}):
        c = cc._toolchain_fingerprint(object())   # different toolchain -> different hash
    assert a == b and a != c
