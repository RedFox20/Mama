"""Pins P1 of the version roadmap: mama refuses a version it cannot read the same way on both sides.
The download reads the mamafile as text, the upload runs it, and a shape they disagree on used to
publish an archive no consumer could ever ask for."""
from unittest.mock import Mock, patch

import pytest

from mama import artifactory as art, papa_upload
from mama.types.git import Git
from testutils import strip_ansi

_ONE = "class P:\n    def settings(self):\n        self.version = '8.0.1'\n"
_TWO = "class P:\n    def settings(self):\n        self.version = '8.0.1'\n        if x: self.version = '8.0.1-lgpl'\n"
_COMPUTED = "class P:\n    def settings(self):\n        self.version = compute()\n"


def _dep(name='libffmpeg'):
    dep = Mock(config=Mock(print=True, verbose=False))
    dep.name = name
    del dep.warned_bad_version  # a Mock answers every attribute, so the warn-once flag must start unset
    return dep


@pytest.mark.parametrize('text, version', [(_ONE, '8.0.1'), (_TWO, ''), (_COMPUTED, ''), ('', '')])
def test_only_one_literal_survives_the_trust_rule(text, version):
    assert Git.trusted_self_version(_dep(), text, 'mamafile.py') == version


@pytest.mark.parametrize('text, reason', [(_TWO, 'more than once'), (_COMPUTED, 'computes')])
def test_a_refused_shape_says_which_one_it_is(text, reason, capsys):
    Git.trusted_self_version(_dep(), text, '/pkg/libffmpeg/mamafile.py')
    out = strip_ansi(capsys.readouterr().out)
    assert reason in out and 'mamafile.py' in out and 'libffmpeg' in out
    assert 'commit hash' in out and 'one raw string literal' in out  # what mama did, and how to fix it


def test_an_unpinned_mamafile_warns_about_nothing(capsys):
    assert Git.trusted_self_version(_dep(), 'class P:\n    pass\n', 'mamafile.py') == ''
    assert capsys.readouterr().out == ''


def test_the_warning_repeats_once_per_dep_not_once_per_probe(capsys):
    dep = _dep()
    for _ in range(3): Git.trusted_self_version(dep, _TWO, 'mamafile.py')  # shim probe, then fetch, then upload
    assert strip_ansi(capsys.readouterr().out).count('more than once') == 1


def test_a_quiet_run_stays_quiet(capsys):
    dep = _dep(); dep.config.print = False
    Git.trusted_self_version(dep, _TWO, 'mamafile.py')
    assert capsys.readouterr().out == ''


# -- the upload guard --------------------------------------------------------------------------------

def _target(executed, on_disk):
    target = Mock(version=executed, dep=Mock())
    target.name = 'libffmpeg'
    target.config = Mock(print=True, verbose=False)
    return target, patch('mama.papa_upload.resolve_pinned_version', return_value=on_disk)


@pytest.mark.parametrize('executed, on_disk', [('8.0.1', '8.0.1'), ('', ''), (None, '')])
def test_an_upload_proceeds_when_both_sides_agree(executed, on_disk):
    target, resolve = _target(executed, on_disk)
    with resolve:
        assert papa_upload._download_can_find_this_version(target)


@pytest.mark.parametrize('executed, on_disk', [
    ('8.0.1-lgpl', '8.0.1'),  # a conditional version: the upload computed one, the reader took the other
    ('2.1.0', ''),            # a computed version: the reader saw nothing, so the download uses the hash
])
def test_an_upload_is_refused_when_the_download_would_look_elsewhere(executed, on_disk, capsys):
    target, resolve = _target(executed, on_disk)
    with resolve:
        assert not papa_upload._download_can_find_this_version(target)
    out = strip_ansi(capsys.readouterr().out)
    assert 'UPLOAD REFUSED' in out and repr(executed) in out
    assert (repr(on_disk) if on_disk else '<commit hash>') in out  # both names, so the fix is obvious


def test_the_refusal_stops_the_upload_before_it_archives(tmp_path):
    target, resolve = _target('8.0.1-lgpl', '8.0.1')
    with resolve, patch('mama.papa_upload.artifactory_archive_name') as name:
        papa_upload.papa_upload_to(target, str(tmp_path))
    name.assert_not_called()  # no zip, no ftp: the whole upload path is skipped


def test_resolve_pinned_version_routes_through_the_trust_rule(tmp_path):
    mamafile = tmp_path / 'mamafile.py'
    mamafile.write_text(_TWO)
    dep = _dep(); dep.mamafile_path.return_value = str(mamafile)
    assert art.resolve_pinned_version(dep) == ''  # not '8.0.1': the reader refuses to guess
