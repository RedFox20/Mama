"""Pins the configure/build split: phase ordering, no-op packaging, custom-build collapse,
thread-safe env, per-target -j."""
import os, contextlib
from unittest.mock import patch
from testutils import make_mock_local_dep
from mama import cmake_configure as cc


def _target(tmp_path, **cfg):
    sub = tmp_path / 'pkg'; sub.mkdir()
    dep = make_mock_local_dep(tmp_path, src_dir=sub, jobs=8, **cfg)
    dep.should_rebuild = True; dep.nothing_to_build = False; dep.from_artifactory = False
    return dep.target, dep


def _wire(t, dep):
    """Patch the cmake/package boundary + dep hooks to record call order in `ev`."""
    es = contextlib.ExitStack(); ev = []
    rec = lambda name: (lambda *a, **k: ev.append(name))
    es.enter_context(patch('mama.cmake_configure.run_config', side_effect=rec('run_config')))
    es.enter_context(patch('mama.cmake_configure.run_build', side_effect=rec('run_build')))
    es.enter_context(patch('mama.cmake_configure.inject_env', side_effect=rec('inject_env')))
    es.enter_context(patch('mama.package.clean_intermediate_files', side_effect=rec('clean')))
    es.enter_context(patch.object(t, 'try_automatic_artifactory_fetch', return_value=None))
    es.enter_context(patch.object(t, '_run_packaging', side_effect=rec('package')))
    es.enter_context(patch.object(dep, 'ensure_cmakelists_exists'))
    es.enter_context(patch.object(dep, 'successful_build', side_effect=rec('successful_build')))
    return es, ev


def test_default_build_runs_configure_in_configure_phase_build_in_build_phase(tmp_path):
    t, dep = _target(tmp_path)
    es, ev = _wire(t, dep)
    with es:
        t.configure_phase()
        assert ev == ['inject_env', 'run_config']  # configure half only
        t.build_phase()
    assert ev == ['inject_env', 'run_config', 'run_build', 'successful_build', 'clean', 'package']


def test_noop_node_skips_cmake_but_still_packages(tmp_path):
    t, dep = _target(tmp_path)
    dep.nothing_to_build = True  # header-only / no work
    es, ev = _wire(t, dep)
    with es:
        t.configure_phase(); t.build_phase()
    assert ev == ['package']  # packaging still runs, in dependency order


def test_custom_build_collapses_into_build_phase(tmp_path):
    t, dep = _target(tmp_path)
    es, ev = _wire(t, dep)
    es.enter_context(patch.object(t, '_has_custom_build', return_value=True))
    es.enter_context(patch.object(t, 'build', side_effect=lambda: ev.append('user_build')))
    with es:
        t.configure_phase()
        assert ev == []  # custom build owns its own configure; configure_phase is a no-op
        t.build_phase()
    assert ev == ['user_build', 'successful_build', 'clean', 'package']
    assert 'run_config' not in ev and 'run_build' not in ev


def test_compute_env_strips_cc_cxx_without_mutating_global(tmp_path, monkeypatch):
    t, _ = _target(tmp_path)
    t.config.get_preferred_compiler_paths = lambda: ('gcc', 'g++', '11')
    monkeypatch.setenv('CC', 'x'); monkeypatch.setenv('CXX', 'y')
    env = cc.compute_env(t)
    assert 'CC' not in env and 'CXX' not in env
    assert os.environ['CC'] == 'x' and os.environ['CXX'] == 'y'  # global env untouched (thread-safe)


def test_per_target_jobs_flow_into_j_flag_without_touching_config(tmp_path):
    t, _ = _target(tmp_path)  # config.jobs = 8, linux
    t._build_jobs = 3
    assert cc._mp_flags(t) == '-j3' and t.config.jobs == 8
    t._build_jobs = None
    assert cc._mp_flags(t) == '-j8'  # falls back to config.jobs


def test_configure_phase_sizes_build_weight_from_tu_count(tmp_path):
    # Regression: the build weight (cores the scheduler reserves) must be known at BUILD launch.
    # configure_phase sets _build_jobs from the TU probe; if it stayed None, every build would
    # fall back to all cores and reserve the whole budget -> builds run one-at-a-time (low CPU).
    t, dep = _target(tmp_path)  # config.jobs = 8
    with open(t.build_dir('compile_commands.json'), 'w') as f:
        f.write('[{"file":"a"},{"file":"b"},{"file":"c"}]')
    es, _ = _wire(t, dep)
    with es:
        t.configure_phase()
    assert t._build_jobs == 3   # small package -> small weight -> many such builds run concurrently


def test_probe_build_jobs_counts_tus_caps_and_falls_back(tmp_path):
    t, _ = _target(tmp_path)  # config.jobs = 8
    cc_json = t.build_dir('compile_commands.json')
    with open(cc_json, 'w') as f: f.write('[{"file":"a"},{"file":"b"},{"file":"c"}]')
    assert t._probe_build_jobs() == 3
    with open(cc_json, 'w') as f: f.write('"file"' * 100)
    assert t._probe_build_jobs() == 8  # capped at config.jobs
    os.remove(cc_json)
    assert t._probe_build_jobs() == 8  # missing file -> fallback to config.jobs
