"""sanitizer_suffix mapping + archive name composition (asan/tsan/ubsan/lsan are mutually incompatible)."""
from types import SimpleNamespace

import pytest

from mama.build_config import BuildConfig
from mama import artifactory as art


def _make_config(sanitize=None):
    # BuildConfig.__init__ runs platform detection and CLI parsing - none of
    # which sanitizer_suffix needs. Skip __init__ and set only what's used.
    cfg = BuildConfig.__new__(BuildConfig)
    cfg.sanitize = sanitize
    return cfg


class TestSanitizerSuffix:
    def test_no_sanitizer_returns_empty(self):
        assert _make_config(None).sanitizer_suffix() == ''
        assert _make_config('').sanitizer_suffix() == ''

    @pytest.mark.parametrize('long_name,short', [
        ('address',   'asan'),
        ('thread',    'tsan'),
        ('leak',      'lsan'),
        ('undefined', 'ubsan'),
        ('memory',    'msan'),
    ])
    def test_single_sanitizer_short_name(self, long_name, short):
        assert _make_config(long_name).sanitizer_suffix() == short

    def test_combined_sanitizers_joined_with_underscore(self):
        # '-' is the field separator in the surrounding archive name, so
        # multiple sanitizers must be joined with something else. We chose '_'.
        assert _make_config('address,undefined').sanitizer_suffix() == 'asan_ubsan'
        assert _make_config('thread,leak').sanitizer_suffix() == 'tsan_lsan'

    def test_combined_order_is_preserved(self):
        # The order the user passes them in is the order in the suffix - so a
        # different ordering produces a different archive name. This is fine:
        # nothing in the build cares about ordering, but reproducibility is
        # easier when the suffix is a deterministic function of the input.
        assert _make_config('undefined,address').sanitizer_suffix() == 'ubsan_asan'

    def test_unknown_sanitizer_passed_through_verbatim(self):
        # If clang adds a new sanitizer we don't know about, we still produce
        # a distinct archive name rather than silently colliding with another.
        assert _make_config('cfi').sanitizer_suffix() == 'cfi'
        assert _make_config('address,cfi').sanitizer_suffix() == 'asan_cfi'

    def test_whitespace_tolerated(self):
        # The CLI passes 'sanitize=address,undefined' as-is, but be defensive
        # in case any callers pass through with whitespace.
        assert _make_config(' address , undefined ').sanitizer_suffix() == 'asan_ubsan'

    def test_empty_segments_skipped(self):
        # Trailing comma or doubled comma must not yield an empty short name.
        assert _make_config('address,').sanitizer_suffix() == 'asan'
        assert _make_config('address,,thread').sanitizer_suffix() == 'asan_tsan'


def _make_target(*, sanitize=None, release=True, arch='x64', version='abc1234'):
    """Stub the BuildTarget surface that artifactory_archive_name touches.

    The real BuildConfig.compiler_version() and get_distro_info() pull from
    the host - we stub them out so the test is platform-independent.
    """
    cfg = _make_config(sanitize)
    cfg.release = release
    cfg.arch = arch
    cfg.compiler_version = lambda: 'gcc14'
    cfg.get_distro_info = lambda: ('linux', '24', 'noble')

    dep_source = SimpleNamespace(
        is_pkg=False, fullname=None,
        is_git=False, is_src=False,
    )
    dep = SimpleNamespace(is_root=False, dep_source=dep_source)

    return SimpleNamespace(
        name='pkg',
        version=version,
        config=cfg,
        dep=dep,
    )


class TestArchiveName:
    def test_no_sanitizer_has_no_suffix(self):
        name = art.artifactory_archive_name(_make_target())
        assert name == 'pkg-linux-24-gcc14-x64-release-abc1234'
        assert 'sanitized' not in name

    def test_asan_carries_asan_suffix(self):
        name = art.artifactory_archive_name(_make_target(sanitize='address'))
        assert name == 'pkg-linux-24-gcc14-x64-release-asan-abc1234'

    def test_tsan_and_asan_produce_distinct_names(self):
        # The whole point of this change: asan and tsan are incompatible
        # runtimes, so their archives MUST have different names.
        asan_name = art.artifactory_archive_name(_make_target(sanitize='address'))
        tsan_name = art.artifactory_archive_name(_make_target(sanitize='thread'))
        assert asan_name != tsan_name
        assert '-asan-' in asan_name
        assert '-tsan-' in tsan_name

    def test_combined_sanitizers_in_name(self):
        name = art.artifactory_archive_name(
            _make_target(sanitize='address,undefined'))
        assert name == 'pkg-linux-24-gcc14-x64-release-asan_ubsan-abc1234'

    def test_debug_with_ubsan(self):
        name = art.artifactory_archive_name(
            _make_target(sanitize='undefined', release=False))
        assert name == 'pkg-linux-24-gcc14-x64-debug-ubsan-abc1234'

    def test_legacy_sanitized_suffix_is_gone(self):
        # Regression guard: if someone reverts to the old '-sanitized' suffix
        # this test fails immediately.
        for s in ['address', 'thread', 'leak', 'undefined', 'address,undefined']:
            name = art.artifactory_archive_name(_make_target(sanitize=s))
            assert 'sanitized' not in name, f'old suffix returned for sanitize={s!r}'
