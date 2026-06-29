"""Pins the configure/build split: phase ordering, no-op packaging, custom-build collapse,
thread-safe env, per-target -j, and generator-agnostic TU counting."""
import os, sys, contextlib, shutil, subprocess
import pytest
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


def test_configure_runs_once_across_phases(tmp_path):
    t, dep = _target(tmp_path)
    es, _ = _wire(t, dep)
    calls = []
    es.enter_context(patch.object(t, 'configure', side_effect=lambda: calls.append(1)))
    with es:
        t.configure_phase(); t.build_phase()  # normal build configures in configure_phase
        t._run_configure_once()               # guard blocks any further configure()
    assert calls == [1]


def test_compute_env_strips_cc_cxx_without_mutating_global(tmp_path, monkeypatch):
    t, _ = _target(tmp_path)
    t.config.get_preferred_compiler_paths = lambda: ('gcc', 'g++', '11')
    monkeypatch.setenv('CC', 'x'); monkeypatch.setenv('CXX', 'y')
    env = cc.compute_env(t)
    assert 'CC' not in env and 'CXX' not in env
    assert os.environ['CC'] == 'x' and os.environ['CXX'] == 'y'  # global env untouched (thread-safe)


def test_per_target_jobs_flow_into_j_flag_without_touching_config(tmp_path):
    t, dep = _target(tmp_path)  # config.jobs = 8, linux
    t._build_jobs = 3
    assert cc._mp_flags(t) == '-j3' and t.config.jobs == 8
    t._build_jobs = None
    assert cc._mp_flags(t) == '-j8'  # falls back to config.jobs
    dep.is_root = True; t._build_jobs = 3
    assert cc._mp_flags(t) == '-j8'  # root ignores the per-target TU sizing, runs at full config.jobs


def test_configure_phase_sizes_build_weight_from_tu_count(tmp_path):
    # Regression: configure_phase must set _build_jobs from the TU probe; left None every build
    # would reserve the whole budget and run one-at-a-time.
    t, dep = _target(tmp_path)  # config.jobs = 8
    with open(t.build_dir('compile_commands.json'), 'w') as f:
        f.write('[{"file":"a"},{"file":"b"},{"file":"c"}]')
    es, _ = _wire(t, dep)
    with es:
        t.configure_phase()
    assert t._build_jobs == 3   # small package -> small weight -> many such builds run concurrently


def test_probe_build_jobs_counts_tus_across_generators_and_falls_back(tmp_path):
    t, _ = _target(tmp_path)  # config.jobs = 8, source tree empty
    ccj, vcx = t.build_dir('compile_commands.json'), t.build_dir('app.vcxproj')
    with open(ccj, 'w') as f: f.write('[{"file":"a"},{"file":"b"},{"file":"c"}]')
    assert t._probe_build_jobs() == 3                       # Ninja/Make: compile_commands.json
    with open(ccj, 'w') as f: f.write('"file"' * 100)
    assert t._probe_build_jobs() == 8                       # capped at config.jobs
    os.remove(ccj)
    with open(vcx, 'w') as f:
        f.write('<ClCompile Include="a"/>\n<ClCompile Include="b"/>\n<ClCompile>settings</ClCompile>')
    assert t._probe_build_jobs() == 2                       # Visual Studio: .vcxproj (settings block not counted)
    os.remove(vcx)
    di = t.build_dir('CMakeFiles/t.dir'); os.makedirs(di)   # Unix Makefiles: DependInfo.cmake, one object per TU
    with open(os.path.join(di, 'DependInfo.cmake'), 'w') as f:
        f.write('"/s/a.cpp" "CMakeFiles/t.dir/a.cpp.o" "gcc" "CMakeFiles/t.dir/a.cpp.o.d"\n'
                '"/s/b.cpp" "CMakeFiles/t.dir/b.cpp.o" "gcc" "CMakeFiles/t.dir/b.cpp.o.d"\n')
    assert t._probe_build_jobs() == 2                       # .o.d depfile entries must NOT be double-counted
    shutil.rmtree(t.build_dir('CMakeFiles'))
    for rel in ('a.cpp', 'b.cc', 'h.hpp', 'build/gen.cpp'):  # cross-platform fallback: count C/C++ sources
        p = os.path.join(t.source_dir(), rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, 'w').close()
    assert t._probe_build_jobs() == 2                       # header + build/ tree skipped, only a.cpp + b.cc
    for rel in ('a.cpp', 'b.cc'): os.remove(os.path.join(t.source_dir(), rel))
    assert t._probe_build_jobs() == 0                       # nothing countable -> 0 reserve (no budget slots)


def test_reserved_cores_is_full_build_jobs_capped_at_total(tmp_path):
    t, _ = _target(tmp_path)  # config.jobs = 8
    t._build_jobs = 40; assert t._reserved_cores() == 8   # heavy build reserves the FULL pool (== its -j)
    t._build_jobs = 3;  assert t._reserved_cores() == 3   # small build keeps its -j
    t._build_jobs = 0;  assert t._reserved_cores() == 0   # unsizable -> reserves nothing
    t._build_jobs = None                                  # unset (serial path / sched_debug): probe once + memoize
    with open(t.build_dir('compile_commands.json'), 'w') as f: f.write('[{"file":"a"},{"file":"b"}]')
    assert t._reserved_cores() == 2 and t._build_jobs == 2


def _cmake_tu_count(t, generator):
    """Generate a real 3-TU CMake project with `generator` (no export -> no compile_commands.json)
    so the probe must use the generator's native artifacts (.vcxproj / DependInfo.cmake)."""
    src, bld = t.source_dir(), t.build_dir()
    with open(os.path.join(src, 'CMakeLists.txt'), 'w') as f:
        f.write('cmake_minimum_required(VERSION 3.16)\nproject(t CXX)\nadd_library(t a.cpp b.cpp c.cpp)\n')
    for n in ('a', 'b', 'c'): open(os.path.join(src, f'{n}.cpp'), 'w').write(f'int {n}(){{return 0;}}\n')
    gen = [] if generator is None else ['-G', generator]
    subprocess.run(['cmake', '-S', src, '-B', bld, *gen], check=True, capture_output=True, timeout=180)
    return t._probe_build_jobs()


@pytest.mark.skipif(not shutil.which('cmake'), reason='needs cmake')
@pytest.mark.skipif(sys.platform != 'linux', reason='Unix Makefiles is the Linux default generator')
def test_probe_counts_real_unix_makefiles_tus(tmp_path):
    assert _cmake_tu_count(_target(tmp_path)[0], 'Unix Makefiles') == 3  # via DependInfo.cmake


@pytest.mark.skipif(not shutil.which('cmake'), reason='needs cmake')
@pytest.mark.skipif(sys.platform != 'win32', reason='Visual Studio generator is Windows-only')
def test_probe_counts_real_visualstudio_tus(tmp_path):
    assert _cmake_tu_count(_target(tmp_path)[0], None) == 3  # default Windows generator (VS) -> .vcxproj
