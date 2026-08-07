"""The git half of version resolution: the sparse-mamafile fetch, and the shim probe's
hash-then-version fallback. The scan itself lives in tests/test_target_version/."""

import contextlib
import subprocess
from unittest.mock import Mock, patch

from mama.types.git import Git
from mama import artifactory as art, mamafile_version


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
        # A parent-repo override is a local path not in the remote repo: return None without any network work.
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
        # PROBE label keeps record_clone from firing. --filter=blob:none + --no-checkout keep the fetch under a kilobyte.
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


def test_the_probe_takes_one_fetch_slot_for_its_clone_and_none_for_the_git_show():
    """The slot caps concurrent git fetches at 8. A `git show` that took one would halve that cap.
    Every probe would then hold two slots, and only one of them talks to a remote."""
    dep, git = _make_dep()
    slots = []

    @contextlib.contextmanager
    def counting_slot():
        slots.append(1)
        yield

    with patch('mama.types.git.ssh_multiplex.fetch_slot', side_effect=counting_slot), \
         patch('mama.types.git.ssh_multiplex.ensure_master_for_url'), \
         patch('mama.types.git.ssh_multiplex.pace_new_connection'), \
         patch('mama.types.git.SubProcess.run', return_value=0), \
         patch('mama.types.git.subprocess.run', return_value=Mock(returncode=0, stdout=b"self.version = '1.0'")):
        assert git.fetch_self_version_from_remote(dep) == '1.0'
    assert len(slots) == 1  # the blob-less clone alone


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
        # The first fetch asks by hash and misses. The second asks by self.version=1.0 and hits.
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
        # A pin makes the FIRST probe version-named. On a miss, no hash-named re-probe: such an archive predates the pin.
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
        # configure() never runs on the download probe path, so the pin must be read from disk
        mf = tmp_path / 'protobuf.py'
        mf.write_text("class protobuf:\n"
                      "    def configure(self):\n"
                      "        self.version = '34.0'\n", encoding='utf-8')
        assert mamafile_version.pinned_version(self._dep_with_mamafile(str(mf))) == '34.0'

    def test_empty_when_mamafile_has_no_pin(self, tmp_path):
        mf = tmp_path / 'mamafile.py'
        mf.write_text("class libfoo:\n    pass\n", encoding='utf-8')
        assert mamafile_version.pinned_version(self._dep_with_mamafile(str(mf))) == ''

    def test_empty_when_mamafile_not_on_disk(self, tmp_path):
        assert mamafile_version.pinned_version(self._dep_with_mamafile(str(tmp_path / 'nope.py'))) == ''

    def test_empty_when_dep_has_no_mamafile_path(self):
        # pre-clone git dep without a parent override: mamafile_path() is None
        assert mamafile_version.pinned_version(self._dep_with_mamafile(None)) == ''
