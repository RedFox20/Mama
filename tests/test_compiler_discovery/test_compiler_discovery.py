"""Pins find_compiler_root against a toolchain whose suffixed name is a symlink to an unsuffixed file."""
import os
from unittest.mock import patch

import pytest

from mama.build_config import BuildConfig

# A name no host carries, so /etc/alternatives and /usr/bin cannot answer the search. The suffix
# logic reads the name back off the resolved file, so the name itself never matters.
CC, CXX = 'mamacc', 'mamacc++'
VERSION = '18.1.3'


def _fake(root, name) -> str:
    """An empty file that stands in for a compiler. The search only stats it, and `_find` answers the
    version probe, so nothing here runs. Windows cannot run the shell script a real fake needs."""
    path = os.path.join(root, name)
    open(path, 'w').close()
    return path


def _link(target, link):
    """Make a symlink, or skip the test. Windows grants that right to an admin or to developer mode alone."""
    try: os.symlink(target, link)
    except OSError as e: pytest.skip(f'this host cannot create a symlink: {e}')


def _find(path, suffixes):
    """Discovery with `path` as the only PATH that can answer. suggested_path names a FILE, so a
    directory reaches the search through PATH alone."""
    config = BuildConfig.__new__(BuildConfig)
    config.verbose = False
    with patch.dict(os.environ, {'PATH': path, 'CXX': ''}), \
         patch.object(BuildConfig, 'get_gcc_clang_fullversion', lambda *a, **kw: VERSION):
        return config.find_compiler_root('', CXX, suffixes, dumpfullversion=False)


def _bin_with(tmp_path, *names) -> str:
    """A bin dir that holds each named file, as a PATH entry."""
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    for name in names: _fake(str(bin_dir), name)
    return str(bin_dir) + '/'


def test_a_suffixed_symlink_answers_the_name_the_real_file_carries(tmp_path):
    # The real chain has two links, bin/cc++-18 -> llvm/bin/cc++ -> llvm/bin/cc, so the suffix belongs
    # to the first link alone and a realpath lands on a name the compiler name does not even start.
    real_bin, link_bin = tmp_path / 'llvm-18' / 'bin', tmp_path / 'bin'
    real_bin.mkdir(parents=True); link_bin.mkdir()
    _fake(str(real_bin), CC)
    _link(CC, real_bin / CXX)                       # relative, as an LLVM install writes it
    for name in (CC, CXX):
        _link(real_bin / name, link_bin / f'{name}-18')

    root, suffix, version = _find(str(link_bin) + '/', ['-18', ''])
    assert version == VERSION
    # the caller composes both paths from these two, and cmake refuses one that does not exist
    assert os.path.exists(f'{root}{CXX}{suffix}'), f'{root}{CXX}{suffix}'
    assert os.path.exists(f'{root}{CC}{suffix}'), f'{root}{CC}{suffix}'


def test_a_real_suffixed_file_keeps_its_suffix(tmp_path):
    # a toolchain whose files really carry the suffix, with no symlink in play
    root, suffix, _ = _find(_bin_with(tmp_path, f'{CC}-13', f'{CXX}-13'), ['-13', ''])
    assert suffix == '-13' and os.path.exists(f'{root}{CXX}{suffix}')


def test_an_unsuffixed_toolchain_keeps_its_own_layout(tmp_path):
    root, suffix, _ = _find(_bin_with(tmp_path, CC, CXX), ['-18', ''])
    assert suffix == '' and os.path.exists(f'{root}{CXX}{suffix}')


def test_the_path_separator_of_this_platform_splits_the_search_roots(tmp_path):
    # Windows separates PATH with `;`, and a split on `:` there also cuts the drive letter off
    bin_dir = _bin_with(tmp_path, CC, CXX)
    with patch('os.pathsep', ';'):
        root, suffix, _ = _find(f'/nonexistent;{bin_dir}', ['-18', ''])
    assert suffix == '' and os.path.exists(f'{root}{CXX}{suffix}')


def test_an_unsuffixed_link_answers_the_suffix_the_real_file_carries(tmp_path):
    # bin/cc++ -> gcc/bin/cc++-14, so the resolved root holds the suffixed spelling and no other
    real_bin, link_bin = tmp_path / 'gcc-14' / 'bin', tmp_path / 'bin'
    real_bin.mkdir(parents=True); link_bin.mkdir()
    for name in (CC, CXX):
        _fake(str(real_bin), f'{name}-14')
        _link(real_bin / f'{name}-14', link_bin / name)

    root, suffix, version = _find(str(link_bin) + '/', ['-14', ''])
    assert version == VERSION
    assert os.path.exists(f'{root}{CXX}{suffix}'), f'{root}{CXX}{suffix}'
    assert os.path.exists(f'{root}{CC}{suffix}'), f'{root}{CC}{suffix}'


def test_the_cxx_env_var_reads_the_suffix_off_the_real_file_too(tmp_path):
    # CXX takes the priority path, which passes no suffix at all, so only the resolved name can answer
    real_bin = tmp_path / 'gcc-14' / 'bin'
    real_bin.mkdir(parents=True)
    for name in (CC, CXX): _fake(str(real_bin), f'{name}-14')
    link = tmp_path / CXX
    _link(real_bin / f'{CXX}-14', link)

    config = BuildConfig.__new__(BuildConfig)
    config.verbose = False
    with patch.dict(os.environ, {'PATH': '', 'CXX': str(link)}), \
         patch.object(BuildConfig, 'get_gcc_clang_fullversion', lambda *a, **kw: VERSION):
        root, suffix, _ = config.find_compiler_root('', CXX, ['-14', ''], dumpfullversion=False)
    assert os.path.exists(f'{root}{CXX}{suffix}'), f'{root}{CXX}{suffix}'
    assert os.path.exists(f'{root}{CC}{suffix}'), f'{root}{CC}{suffix}'


def test_a_link_to_a_target_prefixed_compiler_keeps_a_name_that_exists(tmp_path):
    # bin/cc++ -> gcc/bin/x86_64-linux-gnu-cc++-14, whose name starts with neither the compiler nor
    # any spelling the resolved root holds, so only the link itself names a file that exists
    real_bin, link_bin = tmp_path / 'gcc-14' / 'bin', tmp_path / 'bin'
    real_bin.mkdir(parents=True); link_bin.mkdir()
    for name in (CC, CXX):
        _fake(str(real_bin), f'x86_64-linux-gnu-{name}-14')
        _link(real_bin / f'x86_64-linux-gnu-{name}-14', link_bin / name)

    root, suffix, _ = _find(str(link_bin) + '/', ['-14', ''])
    assert os.path.exists(f'{root}{CXX}{suffix}'), f'{root}{CXX}{suffix}'
    assert os.path.exists(f'{root}{CC}{suffix}'), f'{root}{CC}{suffix}'
