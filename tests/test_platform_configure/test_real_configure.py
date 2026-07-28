"""Runs a REAL cmake configure and build per platform, and checks the object file's target machine.

Slow (a real toolchain per case), so it is excluded from the default run. Run it before a release:
    python -m pytest tests/test_platform_configure -m slow
A platform whose toolchain is not installed on this machine skips.
"""
import os
import struct
import pytest

import testutils

# ELF e_machine values, and the ELF class byte that says 32- or 64-bit. This is what proves a cross
# build actually cross-compiled: the configure command can look perfect and still emit host objects.
_EM = {'x86_64': (62, 2), 'aarch64': (183, 2), 'arm': (40, 1), 'mips': (8, 1)}

# (platform arg, machine, a path that must exist for the toolchain to be installed)
_PLATFORMS = [
    ('linux',   'x86_64',  None),
    ('android', 'aarch64', os.getenv('ANDROID_NDK_HOME') or '/opt/android-sdk/ndk'),
    ('raspi',   'aarch64', '/usr/bin/aarch64-linux-gnu-gcc'),
    ('raspi32', 'arm',     '/usr/bin/arm-linux-gnueabihf-gcc'),
    ('mips',    'mips',    '/usr/bin/mipsel-linux-gnu-gcc'),
    ('oclea',   'aarch64', '/opt/oclea/1.0'),
    ('imx8mp',  'aarch64', '/opt/imdt-imx-xwayland/5.0.4'),
    ('xilinx',  'aarch64', '/opt/petalinux/toolchain'),
]

_CMAKELISTS = '''cmake_minimum_required(VERSION 3.15)
project(probe C CXX)
add_library(probe STATIC src/probe.cpp)
target_include_directories(probe PUBLIC src)
install(TARGETS probe ARCHIVE DESTINATION lib)
'''

_MAMAFILE = '''import mama
class probe(mama.BuildTarget):
    def settings(self):
        self.enable_cxx17()
'''


def _write_probe_project(root):
    (root / 'src').mkdir(parents=True, exist_ok=True)
    (root / 'CMakeLists.txt').write_text(_CMAKELISTS)
    (root / 'src' / 'probe.cpp').write_text('int probe_fn() { return 42; }\n')
    (root / 'mamafile.py').write_text(_MAMAFILE)


def _elf_machine(path) -> tuple:
    """(e_machine, elf class) of an ELF file. The header is fixed-layout, so no `file` tool needed."""
    with open(path, 'rb') as f: header = f.read(20)
    assert header[:4] == b'\x7fELF', f'{path} is not an ELF object'
    return struct.unpack_from('<H', header, 18)[0], header[4]


def _find_object(build_dir):
    for root, _, files in os.walk(build_dir):
        for f in files:
            if f.endswith('.o') or f.endswith('.obj'): return os.path.join(root, f)
    return None


@pytest.mark.slow
@pytest.mark.parametrize('platform,machine,probe_path', _PLATFORMS, ids=[p[0] for p in _PLATFORMS])
def test_configure_and_build_produce_the_right_target_machine(platform, machine, probe_path, tmp_path):
    if probe_path and not os.path.exists(probe_path):
        pytest.skip(f'no {platform} toolchain at {probe_path}')
    _write_probe_project(tmp_path)
    cwd = os.getcwd()
    try:
        os.chdir(tmp_path)
        assert testutils.mama_exec(['build', platform], exit_on_fail=False) == 0
    finally:
        os.chdir(cwd)

    build_dir = tmp_path / 'packages' / 'probe'
    obj = _find_object(build_dir)
    assert obj, f'{platform} built no object file under {build_dir}'
    assert _elf_machine(obj) == _EM[machine], f'{platform} built for the wrong machine: {obj}'
