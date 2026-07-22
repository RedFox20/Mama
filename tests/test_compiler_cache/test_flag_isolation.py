"""Regression (real cmake): the synthetic C+CXX probe seeds single-language projects without poisoning
them - it transplants compiler detection only, never project flags. Skipped without cmake."""
import os, glob, shutil, subprocess
import pytest
from mama import cmake_compiler_cache as cc
from mama.cmake_configure import _SEED_PROJECT

pytestmark = pytest.mark.skipif(not shutil.which('cmake'), reason='needs a real cmake')


def _proj(d, name, langs='CXX'):
    os.makedirs(d, exist_ok=True)
    ext = 'cpp' if 'CXX' in langs else 'c'
    open(os.path.join(d, 'CMakeLists.txt'), 'w').write(
        f'cmake_minimum_required(VERSION 3.15)\nproject({name} {langs})\nadd_library({name} STATIC s.{ext})\n')
    open(os.path.join(d, f's.{ext}'), 'w').write(f'int {name}(){{return 0;}}\n')
    return d


def _configure(src, build, flags=''):
    args = ['cmake'] + ([f'-DCMAKE_CXX_FLAGS={flags}'] if flags else []) + ['-S', src, '-B', build]
    return subprocess.run(args, capture_output=True, text=True)


def _ver_dir(build, lang='CXX'):
    hits = glob.glob(os.path.join(build, 'CMakeFiles', '*', f'CMake{lang}Compiler.cmake'))
    return os.path.dirname(hits[0]) if hits else None


def _probe_seed(tmp_path):
    """What _build_seed_probe does in production: configure a synthetic C+CXX project, publish it."""
    src = str(tmp_path / 'probe_src'); build = str(tmp_path / 'probe_build')
    os.makedirs(src, exist_ok=True)
    open(os.path.join(src, 'CMakeLists.txt'), 'w').write(_SEED_PROJECT)
    r = _configure(src, build)
    assert r.returncode == 0, r.stderr
    ver = _ver_dir(build)
    seed = str(tmp_path / 'seed')
    assert cc.publish(seed, ver, build_dir=build), 'a C+CXX probe must be publishable'
    return seed, os.path.basename(ver)


def _consume(seed, ver, src, build, flags=''):
    cc.inject(seed, build, os.path.join(build, 'CMakeFiles', ver), src)
    return _configure(src, build, flags)


def test_a_single_language_project_configures_from_the_probe_seed(tmp_path):
    # The bug this design fixes: seeding used to copy whichever languages the first real target
    # detected, so a C-only target seeding first left C++ projects with 'CMAKE_CXX_COMPILER not set'.
    seed, ver = _probe_seed(tmp_path)
    for name, langs in (('cxxonly', 'CXX'), ('conly', 'C')):
        src = _proj(str(tmp_path / name), name, langs)
        build = str(tmp_path / f'{name}_build')
        r = _consume(seed, ver, src, build)
        assert r.returncode == 0, f'{langs}-only project failed to configure: {r.stderr}'
        assert 'identification is' not in r.stdout, 'seed should have skipped compiler detection'
        assert subprocess.run(['cmake', '--build', build], capture_output=True).returncode == 0


def test_the_seed_carries_no_project_flags(tmp_path):
    seed, ver = _probe_seed(tmp_path)
    cxx = open(os.path.join(seed, 'CMakeCXXCompiler.cmake')).read()
    assert 'CMAKE_CXX_FLAGS' not in cxx  # only toolchain detection is transplanted

    src = _proj(str(tmp_path / 'b'), 'bbb'); build = str(tmp_path / 'b_build')
    r = _consume(seed, ver, src, build, flags='-DMARKER_B_ONLY')
    assert r.returncode == 0, r.stderr
    cache = open(os.path.join(build, 'CMakeCache.txt')).read()
    assert 'MARKER_B_ONLY' in cache          # B keeps its own flags
    assert 'CMAKE_EXECUTABLE_FORMAT' in cache  # ...and inherits the ABI facts the probe skipped detecting
