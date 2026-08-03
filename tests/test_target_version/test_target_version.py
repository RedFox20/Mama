"""The mamafile_version module: what a mamafile declares, whether mama trusts it, and the upload guard
that refuses to publish a name the download side cannot construct."""
from unittest.mock import Mock, patch

import pytest

from mama import artifactory as art, mamafile_version, papa_upload
from mama.types.git import Git
from testutils import make_archive_name_target, strip_ansi

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
    assert mamafile_version.trusted_version(_dep(), text, 'mamafile.py') == version


@pytest.mark.parametrize('text, reason', [(_TWO, 'more than once'), (_COMPUTED, 'computes')])
def test_a_refused_shape_says_which_one_it_is(text, reason, capsys):
    mamafile_version.trusted_version(_dep(), text, '/pkg/libffmpeg/mamafile.py')
    out = strip_ansi(capsys.readouterr().out)
    assert reason in out and 'mamafile.py' in out and 'libffmpeg' in out
    assert 'commit hash' in out and 'one raw string literal' in out  # what mama did, and how to fix it


def test_an_unpinned_mamafile_warns_about_nothing(capsys):
    assert mamafile_version.trusted_version(_dep(), 'class P:\n    pass\n', 'mamafile.py') == ''
    assert capsys.readouterr().out == ''


def test_the_warning_repeats_once_per_dep_not_once_per_probe(capsys):
    dep = _dep()
    for _ in range(3): mamafile_version.trusted_version(dep, _TWO, 'mamafile.py')  # shim probe, then fetch, then upload
    assert strip_ansi(capsys.readouterr().out).count('more than once') == 1


def test_a_quiet_run_stays_quiet(capsys):
    dep = _dep(); dep.config.print = False
    mamafile_version.trusted_version(dep, _TWO, 'mamafile.py')
    assert capsys.readouterr().out == ''


class TestScanReportsWhatTheMamafileSays:
    @pytest.mark.parametrize('text,expected', [
        ("self.version = '1.0'",               '1.0'),
        ('self.version = "1.60"',              '1.60'),
        ("self.version='2.3.4'",               '2.3.4'),
        ("    self.version = '0.9.1-beta'",    '0.9.1-beta'),
        ("self.version = '1.0' # the version", '1.0'),
        ("self.version = ''",                  ''),  # an explicit empty pin is still one literal
        # the method does not matter, the text does: init(), settings() and configure() all read alike
        ("class P:\n    def init(self):\n        self.version = '7.7'\n", '7.7'),
        ("class P:\n    def settings(self):\n        self.version = '7.7'\n", '7.7'),
        ("class P:\n    def configure(self):\n        self.version = '7.7'\n", '7.7'),
    ])
    def test_one_literal_is_the_only_trustworthy_shape(self, text, expected):
        assert mamafile_version.scan_mamafile(text) == (expected, 1, False)

    @pytest.mark.parametrize('text', [
        "self.version = f'{major}.{minor}'",   # f-string
        "self.version = compute_version()",    # function call
        "self.version = MY_VERSION",           # bare variable
    ])
    def test_a_computed_value_is_reported_not_guessed(self, text):
        assert mamafile_version.scan_mamafile(text) == ('', 0, True)

    @pytest.mark.parametrize('text', [
        "class P:\n    def init(self):\n        self.name = 'libfoo'\n",  # never assigned
        "# self.version = '1.0'",                                         # commented out
        "if self.version == '1.0': pass",                                 # a comparison, not an assignment
        "other.self.version = '1.0'",                                     # a different object
        "self.versions = '1.0'",                                          # a different attribute
        "",
    ])
    def test_no_assignment_reports_nothing(self, text):
        assert mamafile_version.scan_mamafile(text) == ('', 0, False)

    def test_a_conditional_reassignment_is_ambiguous(self):
        # THE case this scan exists for. The reader cannot know which branch runs. An upload that picked
        # '2.0' would publish a name the download never asks for, because the reader used to take '1.0'.
        text = "self.version = '1.0'\nif lgpl: self.version = '2.0'\n"
        assert mamafile_version.scan_mamafile(text) == ('', 2, False)

    def test_a_literal_plus_a_computed_branch_is_ambiguous(self):
        text = "self.version = '8.0.1'\nif lgpl: self.version = compute()\n"
        scan = mamafile_version.scan_mamafile(text)
        assert scan.literals == 1 and scan.computed


class TestScanReadsCodeNotText:
    """The scan parses the mamafile. A line scan counts anything that MENTIONS `self.version`, and then
    refuses the real pin sitting next to it."""

    def test_a_docstring_that_documents_the_field_is_not_an_assignment(self):
        text = ('class P:\n'
                '    """Set self.version = \'9.9.9\' to pin the archive name."""\n'
                '    def settings(self):\n        self.version = \'0.13.0\'\n')
        assert mamafile_version.scan_mamafile(text) == ('0.13.0', 1, False)

    def test_a_string_that_quotes_the_field_is_not_an_assignment(self):
        assert mamafile_version.scan_mamafile('error("self.version = \'9.9\' is required")') == ('', 0, False)

    def test_a_literal_wrapped_over_two_lines_is_still_a_literal(self):
        assert mamafile_version.scan_mamafile("self.version = (\n    '8.0.1')\n") == ('8.0.1', 1, False)

    def test_a_module_level_string_constant_resolves(self):
        # The one name a reader can follow: bound once, at module level, to a string.
        assert mamafile_version.scan_mamafile("V = '1.0'\nclass P:\n    def init(self): self.version = V\n") \
            == ('1.0', 1, False)

    def test_a_constant_bound_twice_resolves_to_nothing(self):
        # Which binding ran last decides the executed value, and the reader must not guess.
        text = "V = '1.0'\nV = compute()\nclass P:\n    def init(self): self.version = V\n"
        assert mamafile_version.scan_mamafile(text) == ('', 0, True)

    def test_an_augmented_assignment_is_computed(self):
        assert mamafile_version.scan_mamafile("self.version += '-rc1'") == ('', 0, True)

    def test_an_unparseable_mamafile_falls_back_to_the_line_scan(self):
        # A mamafile written for a newer Python than the one running mama still gets a best effort.
        assert mamafile_version.scan_mamafile("def f(self)\n    self.version = '1.0'\n") == ('1.0', 1, False)


# -- the upload guard --------------------------------------------------------------------------------

def _target(executed, on_disk):
    target = Mock(version=executed, dep=Mock())
    target.name = 'libffmpeg'
    target.config = Mock(print=True, verbose=False)
    return target, patch('mama.papa_upload.pinned_version', return_value=on_disk)


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


def test_a_consumer_owned_override_mamafile_names_the_package(tmp_path):
    """Why mama needs no consumer-side version argument: `add_git(mamafile='mamadeps/qcoro.py')` points at
    a file in the CONSUMER's repo, and that file is on disk before any clone."""
    override = tmp_path / 'mamadeps'; override.mkdir()
    (override / 'qcoro.py').write_text("import mama\nclass qcoro(mama.BuildTarget):\n"
                                       "    def settings(self):\n        self.version = '8.1.0'\n")
    dep = _dep(); dep.mamafile_path.return_value = str(override / 'qcoro.py')
    assert mamafile_version.pinned_version(dep) == '8.1.0'


def test_an_override_version_beats_the_git_tag():
    # ffmpeg tags n8.1.0, the override says 8.1.0, and the override wins on both sides.
    target = make_archive_name_target(version='8.1.0', git_tag='n8.1.0')
    with patch.object(Git, 'get_commit_hash', return_value='df76b66'):
        assert art.artifactory_archive_name(target).endswith('-8.1.0')


def test_pinned_version_routes_through_the_trust_rule(tmp_path):
    mamafile = tmp_path / 'mamafile.py'
    mamafile.write_text(_TWO)
    dep = _dep(); dep.mamafile_path.return_value = str(mamafile)
    assert mamafile_version.pinned_version(dep) == ''  # not '8.0.1': the reader refuses to guess
