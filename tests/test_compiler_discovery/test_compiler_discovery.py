"""Pins find_compiler_root against a toolchain whose suffixed name is a symlink to an unsuffixed file."""
import os
import stat
from unittest.mock import patch

from mama.build_config import BuildConfig

# A name no host carries, so /etc/alternatives and /usr/bin cannot answer the search. The suffix
# logic reads the name back off the resolved file, so the name itself never matters.
CC, CXX = 'mamacc', 'mamacc++'


def _fake(root, name, version='18.1.3') -> str:
    """An executable that answers -dumpversion, which is what the discovery probe asks."""
    path = os.path.join(root, name)
    with open(path, 'w') as f: f.write(f'#!/bin/sh\necho {version}\n')
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _find(bin_dir, suffixes):
    """Discovery with `bin_dir` as the only root that can answer. suggested_path names a FILE, so the
    dir reaches the search through PATH."""
    config = BuildConfig.__new__(BuildConfig)
    config.verbose = False
    with patch.dict(os.environ, {'PATH': bin_dir, 'CXX': ''}):
        return config.find_compiler_root('', CXX, suffixes, dumpfullversion=False)


def test_a_suffixed_symlink_answers_the_name_the_real_file_carries(tmp_path):
    # /usr/bin/clang++-18 -> /usr/lib/llvm-18/bin/clang++, so the -18 suffix belongs to the link alone
    # the real chain has two links: bin/mamacc++-18 -> llvm/bin/mamacc++ -> llvm/bin/mamacc,
    # so a realpath lands on a name that does not start with the compiler name at all
    real_bin, link_bin = tmp_path / 'llvm-18' / 'bin', tmp_path / 'bin'
    real_bin.mkdir(parents=True); link_bin.mkdir()
    _fake(str(real_bin), CC)
    os.symlink(CC, real_bin / CXX)                       # relative, as an LLVM install writes it
    for name in (CC, CXX):
        os.symlink(real_bin / name, link_bin / f'{name}-18')

    root, suffix, version = _find(str(link_bin) + '/', ['-18', ''])
    assert version == '18.1.3'
    # the caller composes both paths from these two, and cmake refuses one that does not exist
    assert os.path.exists(f'{root}{CXX}{suffix}'), f'{root}{CXX}{suffix}'
    assert os.path.exists(f'{root}{CC}{suffix}'), f'{root}{CC}{suffix}'


def test_a_real_suffixed_file_keeps_its_suffix(tmp_path):
    # a toolchain whose files really carry the suffix, with no symlink in play
    bin_dir = tmp_path / 'bin'; bin_dir.mkdir()
    for name in (f'{CC}-13', f'{CXX}-13'): _fake(str(bin_dir), name)
    root, suffix, _ = _find(str(bin_dir) + '/', ['-13', ''])
    assert suffix == '-13' and os.path.exists(f'{root}{CXX}{suffix}')


def test_an_unsuffixed_toolchain_keeps_its_own_layout(tmp_path):
    bin_dir = tmp_path / 'bin'; bin_dir.mkdir()
    for name in (CC, CXX): _fake(str(bin_dir), name)
    root, suffix, _ = _find(str(bin_dir) + '/', ['-18', ''])
    assert suffix == '' and os.path.exists(f'{root}{CXX}{suffix}')
