"""build_variant_suffix mapping + archive name composition (asan/tsan/ubsan/lsan are mutually
incompatible, so their packages must not share a name)."""
import pytest

from mama import artifactory as art, build_names
from testutils import make_archive_name_target as _make_target, plain_config


def _suffix(sanitize=None, coverage=None):
    return build_names.build_variant_suffix(plain_config(sanitize, coverage))


class TestVariantSuffix:
    def test_a_plain_build_has_no_suffix(self):
        assert _suffix(None) == ''
        assert _suffix('') == ''

    @pytest.mark.parametrize('long_name,short', [
        ('address',   'asan'),
        ('thread',    'tsan'),
        ('leak',      'lsan'),
        ('undefined', 'ubsan'),
        ('memory',    'msan'),
    ])
    def test_single_sanitizer_short_name(self, long_name, short):
        assert _suffix(long_name) == '-' + short

    def test_each_sanitizer_gets_its_own_field(self):
        # One spelling for the build dir and the archive name. Nothing parses these names back into
        # fields, so '-' can separate every token in both.
        assert _suffix('address,undefined') == '-asan-ubsan'
        assert _suffix('thread,leak') == '-tsan-lsan'

    def test_the_sanitizer_order_is_preserved(self):
        # The order the user passes them in is the order in the suffix, so a different ordering produces
        # a different name. Nothing in the build cares, and a deterministic function of the input is
        # easier to reproduce.
        assert _suffix('undefined,address') == '-ubsan-asan'

    def test_unknown_sanitizer_passed_through_verbatim(self):
        # If clang adds a new sanitizer we do not know about, we still produce a distinct name rather
        # than silently colliding with another.
        assert _suffix('cfi') == '-cfi'
        assert _suffix('address,cfi') == '-asan-cfi'

    def test_whitespace_tolerated(self):
        # The CLI passes 'sanitize=address,undefined' as-is, but be defensive
        # in case any callers pass through with whitespace.
        assert _suffix(' address , undefined ') == '-asan-ubsan'

    def test_empty_segments_skipped(self):
        # Trailing comma or doubled comma must not yield an empty short name.
        assert _suffix('address,') == '-asan'
        assert _suffix('address,,thread') == '-asan-tsan'

    def test_coverage_comes_before_the_sanitizers(self):
        assert _suffix(None, 'default') == '-cov'
        assert _suffix('address', 'default') == '-cov-asan'


class TestArchiveName:
    def test_no_sanitizer_has_no_suffix(self):
        name = art.artifactory_archive_name(_make_target())
        assert name == 'pkg-linux-24-gcc14-x64-release-abc1234'
        assert 'sanitized' not in name

    def test_asan_carries_asan_suffix(self):
        name = art.artifactory_archive_name(_make_target(sanitize='address'))
        assert name == 'pkg-linux-24-gcc14-x64-release-asan-abc1234'

    def test_tsan_and_asan_produce_distinct_names(self):
        # The whole point of this field: asan and tsan are incompatible
        # runtimes, so their archives MUST have different names.
        asan_name = art.artifactory_archive_name(_make_target(sanitize='address'))
        tsan_name = art.artifactory_archive_name(_make_target(sanitize='thread'))
        assert asan_name != tsan_name
        assert '-asan-' in asan_name
        assert '-tsan-' in tsan_name

    def test_combined_sanitizers_in_name(self):
        name = art.artifactory_archive_name(_make_target(sanitize='address,undefined'))
        assert name == 'pkg-linux-24-gcc14-x64-release-asan-ubsan-abc1234'

    def test_debug_with_ubsan(self):
        name = art.artifactory_archive_name(_make_target(sanitize='undefined', release=False))
        assert name == 'pkg-linux-24-gcc14-x64-debug-ubsan-abc1234'

    def test_a_coverage_build_no_longer_shares_the_plain_name(self):
        # A coverage build produces instrumented binaries. It used to upload under the plain name and
        # serve them to every consumer that asked for a normal build.
        name = art.artifactory_archive_name(_make_target(coverage='default'))
        assert name == 'pkg-linux-24-gcc14-x64-release-cov-abc1234'

    def test_legacy_sanitized_suffix_is_gone(self):
        # Regression guard: if someone reverts to the old '-sanitized' suffix
        # this test fails immediately.
        for s in ['address', 'thread', 'leak', 'undefined', 'address,undefined']:
            name = art.artifactory_archive_name(_make_target(sanitize=s))
            assert 'sanitized' not in name, f'old suffix returned for sanitize={s!r}'
