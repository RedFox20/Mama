"""Pins that a REAL seeded configure holds every cache entry a full one detected.
Excluded from the default run: `python -m pytest tests/test_compiler_cache -m slow`."""
import os
import shutil
import subprocess

import pytest

from mama.buildsys.cmake import compiler_cache as cc

# Every known GNU binutils tool, both spellings CMake writes. A seeded configure skips that search,
# so a dropped entry reaches the build empty.
_TOOL_KEYS = ('CMAKE_AR', 'CMAKE_RANLIB', 'CMAKE_STRIP', 'CMAKE_LINKER', 'CMAKE_NM', 'CMAKE_OBJDUMP',
              'CMAKE_OBJCOPY', 'CMAKE_READELF', 'CMAKE_ADDR2LINE', 'CMAKE_C_COMPILER_AR',
              'CMAKE_C_COMPILER_RANLIB', 'CMAKE_CXX_COMPILER_AR', 'CMAKE_CXX_COMPILER_RANLIB')

_CMAKELISTS = '''cmake_minimum_required(VERSION 3.20)
project(probe C CXX)
add_library(probe STATIC probe.cpp)
'''


def _configure(src, build, cxx, cc_path) -> dict:
    """Configure `build` for real and return its cache entries, key -> line."""
    subprocess.run(['cmake', '-G', 'Ninja', '-B', build, f'-DCMAKE_CXX_COMPILER={cxx}',
                    f'-DCMAKE_C_COMPILER={cc_path}', src], check=True, capture_output=True, timeout=300)
    lines = open(os.path.join(build, 'CMakeCache.txt')).read().splitlines()
    return {ln.split(':', 1)[0]: ln for ln in lines if ':' in ln}


@pytest.mark.slow
def test_a_seeded_configure_keeps_every_tool_a_full_one_found(tmp_path):
    cxx, cc_path = shutil.which('clang++') or shutil.which('g++'), shutil.which('clang') or shutil.which('gcc')
    if not (shutil.which('cmake') and shutil.which('ninja') and cxx and cc_path): pytest.skip('no toolchain')
    src = tmp_path / 'src'; src.mkdir()
    (src / 'CMakeLists.txt').write_text(_CMAKELISTS)
    (src / 'probe.cpp').write_text('int probe(){return 1;}\n')

    full = _configure(str(src), str(tmp_path / 'full'), cxx, cc_path)
    ver = next(d for d in os.listdir(tmp_path / 'full' / 'CMakeFiles') if d[0].isdigit())
    seed, seeded = str(tmp_path / 'seed'), str(tmp_path / 'seeded')
    assert cc.publish(seed, str(tmp_path / 'full' / 'CMakeFiles' / ver), probe=cxx, build_dir=str(tmp_path / 'full'))
    assert cc.inject(seed, seeded, os.path.join(seeded, 'CMakeFiles', ver), str(src))
    injected = _configure(str(src), seeded, cxx, cc_path)

    # a re-run detection would hide the whole defect, because it writes every entry itself
    assert not os.path.exists(os.path.join(seeded, 'CMakeFiles', ver, 'CompilerIdCXX'))
    for key in (k for k in _TOOL_KEYS if k in full):
        assert injected.get(key) == full[key], key
