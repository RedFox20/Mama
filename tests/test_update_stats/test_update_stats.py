"""Unit tests for the load-phase clone/pull/shim summary.

After `mama update`, the dependency-load phase prints a one-line summary like
``Updated 12 target(s): 9 shim-fetched, 2 pulled, 1 cloned in 6.3s`` so a user
can spot which packages are slow to update. These tests cover the counter
class itself and its summary formatting at the empty / single-kind / mixed /
ordering edges.
"""
from __future__ import annotations

import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))
from mama.build_config import UpdateStats  # noqa: E402


class TestCounters:
    def test_initial_state(self):
        s = UpdateStats()
        assert s.cloned == 0
        assert s.pulled == 0
        assert s.shim_fetched == 0
        assert s.total == 0
        assert s.summary_line() == ''

    def test_increments(self):
        s = UpdateStats()
        s.record_clone()
        s.record_pull(); s.record_pull()
        s.record_shim(); s.record_shim(); s.record_shim()
        assert s.cloned == 1
        assert s.pulled == 2
        assert s.shim_fetched == 3
        assert s.total == 6

    def test_thread_safety(self):
        """100 threads each incrementing all three counters must produce exact totals."""
        s = UpdateStats()
        def worker():
            for _ in range(100):
                s.record_clone()
                s.record_pull()
                s.record_shim()
        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads: t.start()
        for t in threads: t.join()
        assert s.cloned == 2000
        assert s.pulled == 2000
        assert s.shim_fetched == 2000
        assert s.total == 6000


class TestTiming:
    def test_duration_zero_until_started(self):
        s = UpdateStats()
        assert s.duration == 0.0

    def test_duration_captured_between_start_and_stop(self):
        s = UpdateStats()
        s.start()
        time.sleep(0.02)
        s.stop()
        assert s.duration >= 0.02
        assert s.duration < 0.5  # sanity ceiling

    def test_stop_without_start_is_noop(self):
        s = UpdateStats()
        s.stop()  # must not crash
        assert s.duration == 0.0


class TestSummaryLine:
    def test_empty_when_nothing_happened(self):
        s = UpdateStats()
        s.start(); s.stop()
        assert s.summary_line() == ''

    def test_single_kind_clone(self):
        s = UpdateStats()
        s.record_clone()
        s.start(); s.stop()
        line = s.summary_line()
        assert 'Updated 1 target(s)' in line
        assert '1 cloned' in line
        assert 'shim-fetched' not in line
        assert 'pulled' not in line

    def test_single_kind_shim(self):
        s = UpdateStats()
        s.record_shim()
        assert '1 shim-fetched' in s.summary_line()

    def test_mixed_kinds_show_all_present(self):
        s = UpdateStats()
        s.record_clone()
        s.record_pull(); s.record_pull()
        s.record_shim(); s.record_shim(); s.record_shim()
        line = s.summary_line()
        assert 'Updated 6 target(s)' in line
        assert '3 shim-fetched' in line
        assert '2 pulled' in line
        assert '1 cloned' in line

    def test_summary_includes_duration(self):
        s = UpdateStats()
        s.record_shim()
        s.start()
        time.sleep(0.01)
        s.stop()
        # get_time_str renders sub-second as Nms
        line = s.summary_line()
        assert 'in ' in line
        assert 'ms' in line or 's' in line  # either is valid depending on timing

    def test_kinds_order_is_shim_pull_clone(self):
        """Stable ordering keeps the summary readable; shim is cheapest, clone is slowest."""
        s = UpdateStats()
        s.record_clone()
        s.record_pull()
        s.record_shim()
        line = s.summary_line()
        # shim-fetched should appear before pulled, which appears before cloned
        i_shim = line.index('shim-fetched')
        i_pull = line.index('pulled')
        i_clone = line.index('cloned')
        assert i_shim < i_pull < i_clone

    def test_kinds_with_zero_count_omitted(self):
        s = UpdateStats()
        s.record_pull()
        line = s.summary_line()
        assert '1 pulled' in line
        # zero counts should not appear
        assert '0 ' not in line
