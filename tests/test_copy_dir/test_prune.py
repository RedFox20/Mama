"""Pins which subdirs `copy_dir` refuses to walk into."""
import os

from mama.utils.fileio import copy_dir
from testutils import write_files


def test_a_vcs_dir_never_reaches_the_output(tmp_path):
    src = str(tmp_path / 'src')
    write_files(src, {'a.h': 'x\n', '.git/HEAD': 'ref: x\n', '.git/objects/ab/cd': 'x'})
    out = str(tmp_path / 'out')
    copy_dir(src, out, remap_root_dirname=True)
    assert os.path.isfile(f'{out}/a.h')
    assert not os.path.exists(f'{out}/.git')


def test_an_output_dir_inside_the_source_does_not_feed_itself(tmp_path):
    src = str(tmp_path / 'src')
    write_files(src, {'a.h': 'x\n', 'sub/b.h': 'y\n'})
    out = f'{src}/out'   # the copy would otherwise walk into what it just wrote
    copy_dir(src, out, remap_root_dirname=True)
    assert os.path.isfile(f'{out}/a.h') and os.path.isfile(f'{out}/sub/b.h')
    assert not os.path.exists(f'{out}/out')
