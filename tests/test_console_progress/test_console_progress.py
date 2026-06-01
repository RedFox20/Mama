"""Parallel-aware console() finalizer: progress redraws + status lines must not tear."""
import threading

import pytest

from mama.utils import system


@pytest.fixture
def reset_progress_state():
    system._progress_active = False
    yield
    system._progress_active = False


class TestProgressFinalization:
    def test_status_line_after_progress_gets_leading_newline(
            self, capsys, reset_progress_state):
        system.console('\r    |====      | 40% (1s)', end='')
        system.console('  - Target foo SHIM FETCHED')
        out = capsys.readouterr().out
        # The status line must start on its own row, not be glued to the bar.
        assert '40% (1s)\n  - Target foo SHIM FETCHED\n' in out

    def test_progress_redraw_does_not_get_extra_newline(
            self, capsys, reset_progress_state):
        # Repeated \r-redraws of the same progress bar must overwrite each
        # other on the same row; we must NOT inject a newline between them.
        system.console('\r    | 20% |', end='')
        system.console('\r    | 40% |', end='')
        system.console('\r    | 60% |', end='')
        assert capsys.readouterr().out == '\r    | 20% |\r    | 40% |\r    | 60% |'

    def test_progress_final_newline_clears_state(
            self, capsys, reset_progress_state):
        system.console('\r    | 50% |', end='')
        # 100% line ends with default '\n' - it commits the progress.
        system.console('\r    |100% |')
        # Subsequent normal status must NOT get a spurious extra newline.
        system.console('  - Target done')
        assert capsys.readouterr().out == '\r    | 50% |\r    |100% |\n  - Target done\n'

    def test_status_print_without_progress_active_is_unaffected(
            self, capsys, reset_progress_state):
        system.console('hello')
        system.console('world')
        # No leading newline injected when no progress was active.
        assert capsys.readouterr().out == 'hello\nworld\n'

    def test_initial_progress_bar_without_carriage_return_still_tracked(
            self, capsys, reset_progress_state):
        # The first frame of an upload progress bar is printed without \r
        # (e.g. artifactory upload prints '   |> ...| 0 %' with end='').
        # Subsequent status writes must still know to finalize it.
        system.console('   |>          | 0 %', end='')
        system.console('  - Target X')
        assert capsys.readouterr().out == '   |>          | 0 %\n  - Target X\n'


class TestThreadSafety:
    def test_parallel_writers_never_tear_within_a_single_call(
            self, capsys, reset_progress_state):
        msgs = [f'msg-{i:04d}' for i in range(200)]
        def worker(text):
            system.console(text)
        threads = [threading.Thread(target=worker, args=(m,)) for m in msgs]
        for t in threads: t.start()
        for t in threads: t.join()
        out = capsys.readouterr().out
        # Every message must appear intact exactly once on its own line.
        lines = [l for l in out.split('\n') if l]
        assert sorted(lines) == sorted(msgs)
