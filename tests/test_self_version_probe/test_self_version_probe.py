"""Self.version regex + sparse-mamafile probe + shim hash-then-version fallback."""

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
        # multi-line mamafile: the assignment lives inside init()
        ("class P:\n    def init(self):\n        self.version = '7.7'\n", '7.7'),
    ])
    def test_matches_literal_assignment(self, text, expected):
        assert Git.extract_self_version(text) == expected

    @pytest.mark.parametrize('text', [
        # f-string: don't try to evaluate
        "self.version = f'{major}.{minor}'",
        # function call: don't try to evaluate
        "self.version = compute_version()",
        # bare variable
        "self.version = MY_VERSION",
        # never assigned
        "class P:\n    def init(self):\n        self.name = 'libfoo'\n",
        # commented out
        "# self.version = '1.0'",
        # comparison, not assignment (no '=')
        "if self.version == '1.0': pass",
        # empty
        "",
    ])
    def test_returns_none_for_non_literal(self, text):
        assert Git.extract_self_version(text) is None

    def test_first_assignment_wins(self):
        # Defensive: conditional re-assignment in a mamafile - we don't try to handle it.
        text = "self.version = '1.0'\nif something: self.version = '2.0'\n"
        assert Git.extract_self_version(text) == '1.0'


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
    return dep, git


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
