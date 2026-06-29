"""Regression (real cmake): a seed from project A transplants only compiler detection, never project
flags - speeds up B without poisoning it. Skipped without cmake."""
import os, glob, shutil, subprocess
import pytest
from mama import cmake_compiler_cache as cc

pytestmark = pytest.mark.skipif(not shutil.which('cmake'), reason='needs a real cmake')


def _proj(d, name):
    os.makedirs(d, exist_ok=True)
    open(os.path.join(d, 'CMakeLists.txt'), 'w').write(
        f'cmake_minimum_required(VERSION 3.15)\nproject({name} CXX)\nadd_library({name} STATIC s.cpp)\n')
    open(os.path.join(d, 's.cpp'), 'w').write(f'int {name}(){{return 0;}}\n')
    return d


def _configure(src, build, flags):
    return subprocess.run(['cmake', f'-DCMAKE_CXX_FLAGS={flags}', '-S', src, '-B', build],
                          capture_output=True, text=True)


def _ver_dir(build):
    hits = glob.glob(os.path.join(build, 'CMakeFiles', '*', 'CMakeCXXCompiler.cmake'))
    return os.path.dirname(hits[0]) if hits else None


def test_seed_from_project_a_does_not_poison_project_b(tmp_path):
    a_build = str(tmp_path / 'a_build')
    r = _configure(_proj(str(tmp_path / 'a'), 'aaa'), a_build, '-DMARKER_A_ONLY')
    assert r.returncode == 0, r.stderr
    a_ver = _ver_dir(a_build)
    assert a_ver, 'project A did not configure'
    cxx = open(os.path.join(a_ver, 'CMakeCXXCompiler.cmake')).read()
    assert 'MARKER_A_ONLY' not in cxx and 'CMAKE_CXX_FLAGS' not in cxx  # captured file carries no project flags

    seed = str(tmp_path / 'seed')
    assert cc.publish(seed, a_ver)

    b_src = _proj(str(tmp_path / 'b'), 'bbb'); b_build = str(tmp_path / 'b_build')
    cc.inject(seed, b_build, os.path.join(b_build, 'CMakeFiles', os.path.basename(a_ver)), b_src)
    r = _configure(b_src, b_build, '-DMARKER_B_ONLY')
    assert r.returncode == 0, r.stderr
    assert 'identification is' not in r.stdout, 'seed should have skipped compiler detection'

    cache = open(os.path.join(b_build, 'CMakeCache.txt')).read()
    assert 'MARKER_B_ONLY' in cache and 'MARKER_A_ONLY' not in cache  # B keeps its own flags, A's never leak
    assert subprocess.run(['cmake', '--build', b_build, '--config', 'Debug'],
                          capture_output=True).returncode == 0  # and B still builds correctly
