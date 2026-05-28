"""Unit tests for mama.main.run_coverage_report.

These pin down two behaviours that are easy to regress:

1. The gcovr command is built with maximally permissive flags
   (``--gcov-ignore-errors all`` and ``--gcov-ignore-parse-errors all``) and
   the right ``--gcov-executable`` wiring for the gcc-N → gcov-N case.
2. A coverage failure - whether gcovr exits non-zero or ``execute_piped_echo``
   itself raises - must never propagate as a build failure. Coverage is
   best-effort; the CI step that runs tests must not be broken by parse
   errors in third-party headers (e.g. nlohmann/json.hpp under newer gcov
   output containing ``%%%%%`` / ``$$$$$`` / ``-block N`` syntax).
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))
from mama import main as mama_main  # noqa: E402


def _make_target(*, msvc=False, gcc=False, cc_path=None,
                 coverage_report='.', source_dir='/src', build_dir='/build'):
    """Build a stub BuildTarget with just the attributes run_coverage_report touches."""
    config = SimpleNamespace(
        msvc=msvc,
        gcc=gcc,
        cc_path=cc_path,
        coverage_report=coverage_report,
    )
    return SimpleNamespace(
        config=config,
        source_dir=lambda _arg=None: source_dir,
        build_dir=lambda: build_dir,
    )


@pytest.fixture
def capture_gcovr(monkeypatch):
    """Replace execute_piped_echo with a recorder. Default exit status 0."""
    state = {'status': 0, 'calls': []}
    def fake(*, cwd, cmd, echo=True, env=None):
        state['calls'].append({'cwd': cwd, 'cmd': cmd, 'echo': echo})
        return state['status'], ''
    monkeypatch.setattr(mama_main, 'execute_piped_echo', fake)
    return state


class TestPlatformShortCircuit:
    def test_msvc_does_not_invoke_gcovr(self, capture_gcovr, capsys):
        mama_main.run_coverage_report(_make_target(msvc=True))
        assert capture_gcovr['calls'] == [], 'MSVC must short-circuit'
        assert 'not supported' in capsys.readouterr().out.lower()


class TestGcovrCommandShape:
    def test_permissive_parse_error_flags(self, capture_gcovr):
        mama_main.run_coverage_report(_make_target())
        cmd = capture_gcovr['calls'][0]['cmd']
        # Both flags are needed: parse-errors covers UnknownLineType,
        # ignore-errors covers gcov-invocation failures.
        assert '--gcov-ignore-parse-errors all' in cmd
        assert '--gcov-ignore-errors all' in cmd
        # Regression guard: the old value silently let UnknownLineType escape
        # the parser as a non-zero exit. It must not come back.
        assert 'negative_hits.warn' not in cmd

    def test_root_is_source_dir_and_build_dir_is_passed(self, capture_gcovr):
        target = _make_target(source_dir='/proj/src', build_dir='/proj/build')
        mama_main.run_coverage_report(target)
        call = capture_gcovr['calls'][0]
        assert '--root "/proj/src"' in call['cmd']
        assert '"/proj/build"' in call['cmd']
        # gcovr is invoked from the source dir so relative paths in the
        # report resolve consistently.
        assert call['cwd'] == '/proj/src'

    def test_gcov_executable_derived_for_gcc_when_present(self, capture_gcovr, tmp_path):
        # cc_path = /tmp/.../gcc-14 → derived gcov path = /tmp/.../gcov-14
        gcov_path = tmp_path / 'gcov-14'
        gcov_path.write_text('')  # only existence is checked
        gcc_path = tmp_path / 'gcc-14'
        target = _make_target(gcc=True, cc_path=str(gcc_path))
        mama_main.run_coverage_report(target)
        cmd = capture_gcovr['calls'][0]['cmd']
        assert f'--gcov-executable "{gcov_path}"' in cmd

    def test_no_gcov_executable_when_clang(self, capture_gcovr, tmp_path):
        # Even if a matching gcov-N existed, clang must not pick it up - the
        # gcov-14 derived from gcc-14 would be wrong for llvm-cov anyway.
        (tmp_path / 'gcov-14').write_text('')
        target = _make_target(gcc=False, cc_path=str(tmp_path / 'gcc-14'))
        mama_main.run_coverage_report(target)
        assert '--gcov-executable' not in capture_gcovr['calls'][0]['cmd']

    def test_no_gcov_executable_when_derived_path_missing(self, capture_gcovr, tmp_path):
        # gcc points somewhere but the gcov-N sibling doesn't exist - we
        # must not pass a bogus --gcov-executable.
        target = _make_target(gcc=True, cc_path=str(tmp_path / 'gcc-14'))
        mama_main.run_coverage_report(target)
        assert '--gcov-executable' not in capture_gcovr['calls'][0]['cmd']

    def test_no_gcov_executable_when_cc_path_unset(self, capture_gcovr):
        target = _make_target(gcc=True, cc_path=None)
        mama_main.run_coverage_report(target)
        assert '--gcov-executable' not in capture_gcovr['calls'][0]['cmd']


class TestFailureNeverPropagates:
    """The whole point of switching to execute_piped_echo: gcovr's exit code
    must never become mama's exit code."""

    def test_nonzero_exit_is_a_warning_not_a_raise(self, capture_gcovr, capsys):
        capture_gcovr['status'] = 120  # what triggered this whole work
        # Must return normally - no exception escapes.
        mama_main.run_coverage_report(_make_target())
        out = capsys.readouterr().out
        assert 'WARNING' in out
        assert '120' in out

    def test_zero_exit_emits_no_warning(self, capture_gcovr, capsys):
        mama_main.run_coverage_report(_make_target())
        out = capsys.readouterr().out
        assert 'WARNING' not in out
        assert 'ERROR' not in out

    def test_exception_during_exec_is_caught(self, monkeypatch, capsys):
        def boom(**_kw):
            raise RuntimeError('subprocess blew up')
        monkeypatch.setattr(mama_main, 'execute_piped_echo', boom)
        # Even an unexpected RuntimeError from the runner must not propagate.
        mama_main.run_coverage_report(_make_target())
        out = capsys.readouterr().out
        assert 'ERROR' in out
        assert 'subprocess blew up' in out

    @pytest.mark.parametrize('status', [1, 2, 64, 120, 130, 255])
    def test_arbitrary_nonzero_status_never_raises(self, capture_gcovr, status):
        capture_gcovr['status'] = status
        mama_main.run_coverage_report(_make_target())  # must not raise
