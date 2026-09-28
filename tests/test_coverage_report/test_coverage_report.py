"""run_coverage_report: gcovr command shape + failure never propagates as build failure."""
from types import SimpleNamespace

import pytest

from testutils import touch_file
from mama import main as mama_main
from mama.platforms.linux import Linux
from mama.platforms.macos import Macos
from mama.platforms.windows import Windows


def _make_target(*, msvc=False, gcc=False, cc_path=None, platform_class=None,
                 coverage_report='.', source_dir='/src', build_dir='/build'):
    """Build a stub BuildTarget with just the attributes run_coverage_report touches."""
    platform_class = platform_class or (Windows if msvc else Linux)
    config = SimpleNamespace(
        msvc=msvc,
        gcc=gcc,
        clang=not gcc,
        cc_path=cc_path,
        coverage_report=coverage_report,
        arch='x64',
        name=lambda: platform_class.name,
    )
    config.platform = platform_class(config)
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
        # Both flags are needed: parse-errors covers UnknownLineType, ignore-errors covers gcov-invocation failures.
        assert '--gcov-ignore-parse-errors all' in cmd
        assert '--gcov-ignore-errors all' in cmd
        # negative_hits.warn alone lets UnknownLineType escape the parser as a non-zero exit, so it must not return
        assert 'negative_hits.warn' not in cmd

    def test_root_is_source_dir_and_build_dir_is_passed(self, capture_gcovr):
        target = _make_target(source_dir='/proj/src', build_dir='/proj/build')
        mama_main.run_coverage_report(target)
        call = capture_gcovr['calls'][0]
        assert '--root "/proj/src"' in call['cmd']
        assert '"/proj/build"' in call['cmd']
        # gcovr runs from the source dir so relative paths in the report resolve consistently
        assert call['cwd'] == '/proj/src'

    def test_gcov_executable_derived_for_gcc_when_present(self, capture_gcovr, tmp_path):
        # only the file name changes: a gcc-14/ dir in the path stays, and the path gets forward slashes
        bin_dir = tmp_path.resolve() / 'gcc-14' / 'bin'
        touch_file(bin_dir / 'gcov-14')
        mama_main.run_coverage_report(_make_target(gcc=True, cc_path=str(bin_dir / 'gcc-14')))
        assert f"--gcov-executable '{(bin_dir / 'gcov-14').as_posix()}'" in capture_gcovr['calls'][0]['cmd']

    @pytest.mark.parametrize('platform_class', [None, Macos])
    def test_no_gcov_executable_when_clang(self, capture_gcovr, tmp_path, platform_class):
        # a gcov-N derived from gcc-N is wrong for llvm-cov. check_platform sets config.gcc on a clang platform too
        (tmp_path / 'gcov-14').write_text('')
        target = _make_target(gcc=platform_class is Macos, cc_path=str(tmp_path / 'gcc-14'), platform_class=platform_class)
        mama_main.run_coverage_report(target)
        assert '--gcov-executable' not in capture_gcovr['calls'][0]['cmd']

    def test_no_gcov_executable_when_derived_path_missing(self, capture_gcovr, tmp_path):
        # gcc points somewhere but the gcov-N sibling does not exist: never pass a bogus --gcov-executable
        target = _make_target(gcc=True, cc_path=str(tmp_path / 'gcc-14'))
        mama_main.run_coverage_report(target)
        assert '--gcov-executable' not in capture_gcovr['calls'][0]['cmd']

    def test_no_gcov_executable_when_cc_path_unset(self, capture_gcovr):
        target = _make_target(gcc=True, cc_path=None)
        mama_main.run_coverage_report(target)
        assert '--gcov-executable' not in capture_gcovr['calls'][0]['cmd']


class TestFailureNeverPropagates:
    def test_nonzero_exit_is_a_warning_not_a_raise(self, capture_gcovr, capsys):
        capture_gcovr['status'] = 120
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
        mama_main.run_coverage_report(_make_target())
        out = capsys.readouterr().out
        assert 'ERROR' in out
        assert 'subprocess blew up' in out

    @pytest.mark.parametrize('status', [1, 2, 64, 120, 130, 255])
    def test_arbitrary_nonzero_status_never_raises(self, capture_gcovr, status):
        capture_gcovr['status'] = status
        mama_main.run_coverage_report(_make_target())  # must not raise
