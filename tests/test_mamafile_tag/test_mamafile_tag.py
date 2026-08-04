"""Pins recipe-change detection: the mtime answers first, the sha1 decides, and the reason names a
mamafile the reader can open."""
import os
from unittest.mock import Mock

import pytest
from testutils import make_mock_dep

from mama import parse_mamafile as pm
from mama.util import file_sha1, short_path

CONFIG = Mock(verbose=False)


def _mamafile(tmp_path, text='class foo: pass\n', mtime=1_000_000_000):
    f = tmp_path / 'mamafile.py'
    f.write_text(text)
    os.utime(f, (mtime, mtime))
    return str(f), str(tmp_path / 'build' / 'mamafile_tag')


def _tag_lines(tagfile):
    return open(tagfile).read().split('\n')


def test_the_first_run_records_the_tag_and_builds(tmp_path):
    file, tag = _mamafile(tmp_path)
    assert pm.update_modification_tag(CONFIG, file, tag)
    assert _tag_lines(tag) == ['1000000000', file_sha1(file)]


def test_an_unchanged_file_never_opens(tmp_path, monkeypatch):
    # the fast layer: one stat per dep per run, so a big CMakeLists is not read for nothing
    file, tag = _mamafile(tmp_path)
    pm.update_modification_tag(CONFIG, file, tag)
    monkeypatch.setattr(pm, 'file_sha1', lambda _: pytest.fail('read the file on the fast path'))
    assert not pm.update_modification_tag(CONFIG, file, tag)


def test_a_touched_file_with_the_same_content_does_not_build(tmp_path):
    # git checkout rewrites a file with identical bytes, and mtime alone rebuilt every consumer
    file, tag = _mamafile(tmp_path)
    pm.update_modification_tag(CONFIG, file, tag)
    os.utime(file, (2_000_000_000, 2_000_000_000))
    assert not pm.update_modification_tag(CONFIG, file, tag)
    assert _tag_lines(tag)[0] == '2000000000'  # re-stamped, so the next run takes the fast path again


def test_an_edited_file_builds(tmp_path):
    file, tag = _mamafile(tmp_path)
    pm.update_modification_tag(CONFIG, file, tag)
    open(file, 'w').write('class foo: pass\n# one more option\n')
    assert pm.update_modification_tag(CONFIG, file, tag)


def test_a_tag_from_an_older_mama_upgrades_without_a_rebuild(tmp_path):
    # an mtime-only tag: the same mtime still means unchanged, so nobody pays a rebuild for the upgrade
    file, tag = _mamafile(tmp_path)
    os.makedirs(os.path.dirname(tag), exist_ok=True)
    open(tag, 'w').write('1000000000')
    assert not pm.update_modification_tag(CONFIG, file, tag)
    assert _tag_lines(tag) == ['1000000000', file_sha1(file)]


def test_a_tag_from_an_older_mama_still_builds_on_a_moved_mtime(tmp_path):
    file, tag = _mamafile(tmp_path)
    os.makedirs(os.path.dirname(tag), exist_ok=True)
    open(tag, 'w').write('999')  # no hash to compare against, so the old rule decides once
    assert pm.update_modification_tag(CONFIG, file, tag)


def test_a_missing_file_never_builds(tmp_path):
    assert not pm.update_modification_tag(CONFIG, str(tmp_path / 'gone.py'), str(tmp_path / 'tag'))


def test_a_custom_mamafile_is_named_by_its_own_path(tmp_path):
    # the consumer set mamafile='mamadeps/qcoro.py', so `qcoro/mamafile.py modified` named nothing
    dep = make_mock_dep(tmp_path, name='qcoro', mamafile='mamadeps/qcoro.py')
    assert short_path(dep.mamafile_path()) == 'mamadeps/qcoro.py'


def test_a_mamafile_inside_the_clone_keeps_the_dep_name_form(tmp_path):
    assert short_path(make_mock_dep(tmp_path, name='qcoro').mamafile_path()) == 'qcoro/mamafile.py'


def test_no_mamafile_path_names_nothing():
    assert short_path(None) == ''
