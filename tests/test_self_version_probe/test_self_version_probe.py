"""Self.version regex + sparse-mamafile probe + shim hash-then-version fallback."""

import contextlib
import subprocess
from unittest.mock import Mock, patch

import pytest

from mama.types.git import Git
from mama import artifactory as art


class TestExtractSelfVersion:
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
        assert Git.extract_self_version(text) == (expected, 1, False)

    @pytest.mark.parametrize('text', [
        "self.version = f'{major}.{minor}'",   # f-string
        "self.version = compute_version()",    # function call
        "self.version = MY_VERSION",           # bare variable
    ])
    def test_a_computed_value_is_reported_not_guessed(self, text):
        assert Git.extract_self_version(text) == ('', 0, True)

    @pytest.mark.parametrize('text', [
        "class P:\n    def init(self):\n        self.name = 'libfoo'\n",  # never assigned
        "# self.version = '1.0'",                                         # commented out
        "if self.version == '1.0': pass",                                 # a comparison, not an assignment
        "other.self.version = '1.0'",                                     # a different object
        "self.versions = '1.0'",                                          # a different attribute
        "",
    ])
    def test_no_assignment_reports_nothing(self, text):
        assert Git.extract_self_version(text) == ('', 0, False)

    def test_a_conditional_reassignment_is_ambiguous(self):
        # THE case this scan exists for. The reader cannot know which branch runs. An upload that picked
        # '2.0' would publish a name the download never asks for, because the reader used to take '1.0'.
        text = "self.version = '1.0'\nif lgpl: self.version = '2.0'\n"
        assert Git.extract_self_version(text) == ('', 2, False)

    def test_a_literal_plus_a_computed_branch_is_ambiguous(self):
        text = "self.version = '8.0.1'\nif lgpl: self.version = compute()\n"
        scan = Git.extract_self_version(text)
        assert scan.literals == 1 and scan.computed


class TestScanReadsCodeNotText:
    """The scan parses the mamafile. A line scan counts anything that MENTIONS `self.version`, and then
    refuses the real pin sitting next to it."""

    def test_a_docstring_that_documents_the_field_is_not_an_assignment(self):
        text = ('class P:\n'
                '    """Set self.version = \'9.9.9\' to pin the archive name."""\n'
                '    def settings(self):\n        self.version = \'0.13.0\'\n')
        assert Git.extract_self_version(text) == ('0.13.0', 1, False)

    def test_a_string_that_quotes_the_field_is_not_an_assignment(self):
        assert Git.extract_self_version('error("self.version = \'9.9\' is required")') == ('', 0, False)

    def test_a_literal_wrapped_over_two_lines_is_still_a_literal(self):
        assert Git.extract_self_version("self.version = (\n    '8.0.1')\n") == ('8.0.1', 1, False)

    def test_a_module_level_string_constant_resolves(self):
        # The one name a reader can follow: bound once, at module level, to a string.
        assert Git.extract_self_version("V = '1.0'\nclass P:\n    def init(self): self.version = V\n") \
            == ('1.0', 1, False)

    def test_a_constant_bound_twice_resolves_to_nothing(self):
        # Which binding ran last decides the executed value, and the reader must not guess.
        text = "V = '1.0'\nV = compute()\nclass P:\n    def init(self): self.version = V\n"
        assert Git.extract_self_version(text) == ('', 0, True)

    def test_an_augmented_assignment_is_computed(self):
        assert Git.extract_self_version("self.version += '-rc1'") == ('', 0, True)

    def test_an_unparseable_mamafile_falls_back_to_the_line_scan(self):
        # A mamafile written for a newer Python than the one running mama still gets a best effort.
        assert Git.extract_self_version("def f(self)\n    self.version = '1.0'\n") == ('1.0', 1, False)


def _make_dep(branch='main', mamafile_field=''):
    config = Mock()
    config.artifactory_ftp = 'ftp.example.com'
    config.verbose = False
    config.print = False
    config.is_network_available.return_value = True
    config.update_stats = Mock()
    config.target_matches.return_value = False

    git = Git(name='libfoo', url='https://example.com/libfoo.git',
              branch=branch, tag='', mamafile=mamafile_field,
              shallow=True, args=[])
    dep = Mock()
    dep.name = 'libfoo'
    dep.config = config
    dep.dep_source = git
    dep.target_args = []
    dep.from_artifactory = False
    dep.write_shim_marker = Mock()
    dep.mamafile = None  # no parent-repo mamafile override (the common case)
    return dep, git


class _FakeClock:
    def __init__(self, values):
        self.values = iter(values)
        self.last = 0.0

    def __call__(self):
        self.last = next(self.values, self.last)
        return self.last


def _run_filtered_progress_lines(lines, monotonic_values):
    dep, git = _make_dep()
    dep.config.print = True
    printed = []

    def fake_run(cmd, io_func=None, **_kwargs):
        for line in lines:
            io_func(Mock(), line)
        return 0

    with patch('mama.types.git.time.monotonic', new=_FakeClock(monotonic_values)), \
         patch('mama.types.git.progress', side_effect=lambda text, **_kw: printed.append(text)), \
         patch('mama.types.git.SubProcess.run', side_effect=fake_run), \
         patch('mama.types.git.ssh_multiplex.ensure_master_for_url'), \
         patch('mama.types.git.ssh_multiplex.pace_new_connection'), \
         patch('mama.types.git.ssh_multiplex.fetch_slot',
               side_effect=lambda: contextlib.nullcontext()):
        git._run_git_with_filtered_progress(dep, 'git clone fake target', label='PROBE')
    return printed


class TestFetchSelfVersionFromRemote:
    def _patch_clone(self, return_code=0):
        return patch.object(Git, '_run_git_with_filtered_progress',
                            new=lambda *a, **k: (return_code, '', '100ms'))

    def _patch_show(self, stdout=b'', returncode=0):
        return patch('mama.types.git.subprocess.run',
                     return_value=Mock(returncode=returncode, stdout=stdout))

    def test_returns_version_when_mamafile_has_literal(self):
        dep, git = _make_dep()
        with self._patch_clone(), self._patch_show(stdout=b"self.version = '1.60'"):
            assert git.fetch_self_version_from_remote(dep) == '1.60'

    def test_returns_none_when_clone_fails(self):
        dep, git = _make_dep()
        with self._patch_clone(return_code=128), \
             patch('mama.types.git.subprocess.run') as mock_show:
            assert git.fetch_self_version_from_remote(dep) is None
            mock_show.assert_not_called()

    def test_returns_none_when_git_show_fails(self):
        dep, git = _make_dep()
        with self._patch_clone(), self._patch_show(returncode=128):
            assert git.fetch_self_version_from_remote(dep) is None

    def test_returns_none_on_show_timeout(self):
        dep, git = _make_dep()
        with self._patch_clone(), \
             patch('mama.types.git.subprocess.run',
                   side_effect=subprocess.TimeoutExpired(cmd='git', timeout=30)):
            assert git.fetch_self_version_from_remote(dep) is None

    def test_returns_none_when_network_unavailable(self):
        dep, git = _make_dep()
        dep.config.is_network_available.return_value = False
        with patch.object(Git, '_run_git_with_filtered_progress') as mock_clone, \
             patch('mama.types.git.subprocess.run') as mock_show:
            assert git.fetch_self_version_from_remote(dep) is None
            mock_clone.assert_not_called()
            mock_show.assert_not_called()

    def test_returns_none_for_local_mamafile_override(self):
        # A parent-repo override (dep.mamafile is a resolved local path) is not in the remote
        # repo: `git show HEAD:<local path>` can never work, and the local file was already
        # checked by resolve_pinned_version. Must return None without any network work.
        dep, git = _make_dep()
        dep.mamafile = 'C:/parent/mamadeps/libfoo.py'
        with patch.object(Git, '_run_git_with_filtered_progress') as mock_clone, \
             patch('mama.types.git.subprocess.run') as mock_show:
            assert git.fetch_self_version_from_remote(dep) is None
            mock_clone.assert_not_called()
            mock_show.assert_not_called()

    def test_uses_custom_mamafile_path_when_dep_specifies_one(self):
        dep, git = _make_dep(mamafile_field='subdir/mama_alt.py')
        captured = {}
        def fake_show(cmd, **kw):
            captured['cmd'] = cmd
            return Mock(returncode=0, stdout=b"self.version = '3.1'")
        with self._patch_clone(), patch('mama.types.git.subprocess.run', side_effect=fake_show):
            assert git.fetch_self_version_from_remote(dep) == '3.1'
        assert 'HEAD:subdir/mama_alt.py' in captured['cmd']

    def test_uses_blobless_no_checkout_clone_and_probe_label(self):
        # PROBE label keeps update_stats.record_clone from firing for what isn't a real clone.
        # --filter=blob:none + --no-checkout keep the fetch under a kilobyte.
        dep, git = _make_dep()
        captured = {}
        def fake_clone(self_, dep_, cmd, label):
            captured['cmd'], captured['label'] = cmd, label
            return 0, '', '100ms'
        with patch.object(Git, '_run_git_with_filtered_progress', new=fake_clone), \
             self._patch_show(stdout=b"self.version = '1.0'"):
            git.fetch_self_version_from_remote(dep)
        assert '--filter=blob:none' in captured['cmd']
        assert '--no-checkout' in captured['cmd']
        assert '--depth=1' in captured['cmd']
        assert captured['label'] == 'PROBE'


class TestFilteredGitProgress:
    def test_progress_waits_five_ms_from_first_non_completion_report(self):
        lines = [
            'Receiving objects:   3% (1/30)',
            'Receiving objects:   6% (2/30)',
            'Receiving objects:   9% (3/30)',
            'Receiving objects:  12% (4/30)',
            'Receiving objects: 100% (30/30)',
        ]

        printed = _run_filtered_progress_lines(
            lines,
            [0.0, 1.000, 1.004, 1.006, 1.007, 1.008, 1.009])

        assert len(printed) == 2
        assert 'receiving objects' in printed[0] and '  9%' in printed[0]
        assert 'receiving objects' in printed[1] and '100%' in printed[1]

    def test_completion_is_reported_for_each_progress_stage_inside_delay(self):
        lines = [
            'remote: Counting objects: 100% (30/30), done.',
            'remote: Compressing objects: 100% (22/22), done.',
            'Receiving objects: 100% (30/30)',
        ]

        printed = _run_filtered_progress_lines(
            lines,
            [0.0, 0.001, 0.002, 0.003, 0.004])

        assert len(printed) == 3
        assert 'counting objects' in printed[0] and '100%' in printed[0]
        assert 'compressing objects' in printed[1] and '100%' in printed[1]
        assert 'receiving objects' in printed[2] and '100%' in printed[2]


_PROBE_TARGET = lambda **kw: Mock(name='probe', version=None)


class TestShimProbeFallback:
    def test_hash_hit_skips_version_probe(self):
        dep, _ = _make_dep()
        with patch.object(Git, 'init_commit_hash', return_value='abc1234'), \
             patch.object(Git, 'fetch_self_version_from_remote') as mock_version, \
             patch('mama.artifactory.artifactory_fetch_and_reconfigure', return_value=(True, [])), \
             patch('mama.artifactory.artifactory_archive_name', return_value='libfoo-x-abc1234'), \
             patch('mama.build_target.BuildTarget', side_effect=_PROBE_TARGET):
            target, _ = art.try_load_artifactory_shim(dep)
        assert target is not None
        mock_version.assert_not_called()

    def test_hash_miss_falls_through_to_version_probe(self):
        # First fetch is hash-based (miss); second uses self.version=1.0 (hit).
        dep, _ = _make_dep()
        fetch_versions = []
        def fake_fetch(target):
            v = getattr(target, 'version', None)
            fetch_versions.append(v)
            return (True, []) if v == '1.0' else (False, None)
        with patch.object(Git, 'init_commit_hash', return_value='abc1234'), \
             patch.object(Git, 'fetch_self_version_from_remote', return_value='1.0') as mock_version, \
             patch('mama.artifactory.artifactory_fetch_and_reconfigure', side_effect=fake_fetch), \
             patch('mama.artifactory.artifactory_archive_name', return_value='libfoo-x-1.0'), \
             patch('mama.build_target.BuildTarget', side_effect=_PROBE_TARGET):
            target, _ = art.try_load_artifactory_shim(dep)
        assert target is not None
        mock_version.assert_called_once_with(dep)
        assert fetch_versions == [None, '1.0']

    def test_pinned_probe_miss_gets_no_fallback(self):
        # A local mamafile pin makes the FIRST probe version-named (applied inside
        # fetch_and_reconfigure). On a miss there must be no hash-named re-probe: any
        # hash-named archive predates the pin, which was bumped precisely to bury it.
        dep, _ = _make_dep()
        def fake_fetch(target):
            target.version = '34.0'  # what resolve_pinned_version does on the real path
            return (False, None)
        with patch.object(Git, 'init_commit_hash', return_value='abc1234'), \
             patch.object(Git, 'fetch_self_version_from_remote') as mock_version, \
             patch('mama.artifactory.artifactory_fetch_and_reconfigure', side_effect=fake_fetch), \
             patch('mama.build_target.BuildTarget', side_effect=_PROBE_TARGET):
            target, _ = art.try_load_artifactory_shim(dep)
        assert target is None
        mock_version.assert_not_called()

    def test_hash_miss_and_no_self_version_returns_none(self):
        dep, _ = _make_dep()
        with patch.object(Git, 'init_commit_hash', return_value='abc1234'), \
             patch.object(Git, 'fetch_self_version_from_remote', return_value=None), \
             patch('mama.artifactory.artifactory_fetch_and_reconfigure', return_value=(False, None)), \
             patch('mama.build_target.BuildTarget', side_effect=_PROBE_TARGET):
            target, _ = art.try_load_artifactory_shim(dep)
        assert target is None
        assert dep.from_artifactory is False  # must reset so caller's clone path runs cleanly

    def test_hash_miss_with_self_version_but_still_no_archive_returns_none(self):
        dep, _ = _make_dep()
        with patch.object(Git, 'init_commit_hash', return_value='abc1234'), \
             patch.object(Git, 'fetch_self_version_from_remote', return_value='9.9'), \
             patch('mama.artifactory.artifactory_fetch_and_reconfigure', return_value=(False, None)), \
             patch('mama.build_target.BuildTarget', side_effect=_PROBE_TARGET):
            target, _ = art.try_load_artifactory_shim(dep)
        assert target is None


class TestResolvePinnedVersion:
    def _dep_with_mamafile(self, path):
        dep = Mock()
        dep.mamafile_path.return_value = path
        return dep

    def test_reads_pin_from_mamafile_on_disk(self, tmp_path):
        # version pinned inside configure(): never executed on the download probe path,
        # which is exactly why it must be read from disk instead.
        mf = tmp_path / 'protobuf.py'
        mf.write_text("class protobuf:\n"
                      "    def configure(self):\n"
                      "        self.version = '34.0'\n", encoding='utf-8')
        assert art.resolve_pinned_version(self._dep_with_mamafile(str(mf))) == '34.0'

    def test_empty_when_mamafile_has_no_pin(self, tmp_path):
        mf = tmp_path / 'mamafile.py'
        mf.write_text("class libfoo:\n    pass\n", encoding='utf-8')
        assert art.resolve_pinned_version(self._dep_with_mamafile(str(mf))) == ''

    def test_empty_when_mamafile_not_on_disk(self, tmp_path):
        assert art.resolve_pinned_version(self._dep_with_mamafile(str(tmp_path / 'nope.py'))) == ''

    def test_empty_when_dep_has_no_mamafile_path(self):
        # pre-clone git dep without a parent override: mamafile_path() is None
        assert art.resolve_pinned_version(self._dep_with_mamafile(None)) == ''
