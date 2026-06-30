"""Pins Stage 2 build-insights: timetrace tree rebuild, frontend/backend/link aggregation, header
costs, target scoping, and the vcperf session start/stop wiring."""
from types import SimpleNamespace
from mama import build_insights as bi
from testutils import strip_ansi

def _b(name, ts, args=None): return {'ph': 'B', 'name': name, 'ts': ts, **({'args': args} if args else {})}
def _e(ts): return {'ph': 'E', 'ts': ts}
def _x(name, ts, dur): return {'ph': 'X', 'name': name, 'ts': ts, 'dur': dur}

# CL Invocation 0 (a.cpp -> C:\proj\build): frontend=100us (a.cpp incl big.h), backend=60us, +someFunc codegen.
# CL Invocation 1 (b.cpp -> C:\other\build): frontend=60us, re-parses big.h. Link 0 -> C:\proj\build, 50us.
_TRACE = {'traceEvents': [
    _b('CL Invocation 0', 0, {'File Input': r'C:\proj\src\a.cpp', 'File Output': r'C:\proj\build\a.obj'}),
      _b('Pass1', 0),
        _b(r'C:\proj\src\a.cpp', 0), _x(r'C:\proj\inc\big.h', 10, 60), _e(100),
      _e(100),
      _b('Pass2', 100), _x('someFunc', 100, 40), _e(160),
    _e(160),
    _b('CL Invocation 1', 200, {'File Input': r'C:\other\src\b.cpp', 'File Output': r'C:\other\build\b.obj'}),
      _b('Pass1', 200),
        _b(r'C:\other\src\b.cpp', 200), _x(r'C:\proj\inc\big.h', 210, 20), _e(260),
      _e(260),
    _e(260),
    _b('Link Invocation 0', 260, {'File Output': r'C:\proj\build\app.exe', 'File Input': r'C:\proj\build\a.obj'}),
    _e(310),
]}

US = 1e-6


def test_build_tree_reconstructs_nesting_and_durations():
    roots = bi._build_tree(_TRACE['traceEvents'])
    assert [r.name for r in roots] == ['CL Invocation 0', 'CL Invocation 1', 'Link Invocation 0']
    assert roots[0].dur == 160 and roots[0].children[0].name == 'Pass1'
    assert roots[0].children[0].children[0].dur == 100   # a.cpp B/E span encloses its include


def test_root_totals_and_frontend_backend_split():
    s = bi.parse_timetrace(_TRACE)
    assert s.n_tu == 2
    assert s.compile_s == (160 + 60) * US and s.link_s == 50 * US
    assert s.frontend_s == (100 + 60) * US and s.backend_s == 60 * US
    assert s.frontend_s + s.backend_s == s.compile_s   # backend is compile minus outermost-file parse


def test_header_costs_count_includes_not_tu_sources():
    files = dict((b, (sec, n)) for b, sec, n in bi.parse_timetrace(_TRACE).files)
    assert files['big.h'] == (80 * US, 2)   # included by both TUs -> summed, counted twice
    assert 'a.cpp' not in files and 'b.cpp' not in files   # a TU's own source is not a header


def test_codegen_symbols_exclude_structural_and_paths():
    syms = [name for name, _ in bi.parse_timetrace(_TRACE).symbols]
    assert syms == ['someFunc']   # Pass1/Pass2 structural and file paths are not codegen symbols


def test_scope_filters_to_one_packages_tus():
    s = bi.parse_timetrace(_TRACE, scope_paths=[r'C:\proj'])
    assert s.n_tu == 1 and s.link_s == 50 * US        # a.cpp + its link, not b.cpp under C:\other
    assert [t[0] for t in s.tus] == ['a.cpp']
    assert dict((b, n) for b, _, n in s.files)['big.h'] == 1   # only a.cpp's parse of it


def test_is_path_separates_files_from_symbols():
    assert bi._is_path(r'C:\x\y.h') and bi._is_path('/usr/include/vector')
    assert not bi._is_path('std::vector<int>::push_back') and not bi._is_path('someFunc')


def test_empty_trace_reports_no_activity(capsys):
    s = bi.parse_timetrace({'traceEvents': []})
    assert s.empty
    bi.print_buildtimes_deep(s, 'root')
    assert 'no compiler activity captured' in strip_ansi(capsys.readouterr().out)


def test_deep_report_renders_all_sections(capsys):
    bi.print_buildtimes_deep(bi.parse_timetrace(_TRACE), 'root')
    out = strip_ansi(capsys.readouterr().out)
    for token in ['Build Insights (root)', 'frontend', 'backend', 'link', 'translation units', 'a.cpp',
                  'costliest headers', 'big.h']:
        assert token in out


def test_find_vcperf_prefers_env_override(monkeypatch, tmp_path):
    exe = tmp_path / 'vcperf.exe'; exe.write_text('')
    monkeypatch.setenv('VCPERF', str(exe))
    assert bi.find_vcperf(SimpleNamespace()) == str(exe)


def test_find_vcperf_returns_empty_when_absent(monkeypatch):
    monkeypatch.delenv('VCPERF', raising=False)
    monkeypatch.setattr(bi.util, 'find_executable_from_system', lambda name: '')
    def _raise(): raise EnvironmentError('no VS')
    cfg = SimpleNamespace(get_msvc_bin64=_raise, get_visualstudio_path=_raise)
    assert bi.find_vcperf(cfg) == ''


def test_session_issues_start_noadmin_nocpusampling_and_stop_timetrace(monkeypatch, tmp_path):
    cmds, out = [], tmp_path / 'tt.json'
    def _run(args, **k): cmds.append(args); out.write_text('{}'); return 0  # /stop writes the trace
    monkeypatch.setattr(bi.SubProcess, 'run', staticmethod(_run))
    with bi.VcPerfSession('vcperf.exe', str(out)) as s:
        assert s.ok
    assert s.ok   # judged by the written file, not the exit code
    flat = [' '.join(c) for c in cmds]
    assert any('/start /noadmin /nocpusampling /level3 mama_buildtimes' in c for c in flat)
    assert any(f'/stop mama_buildtimes /timetrace {out}' in c for c in flat)


def test_session_missing_trace_file_degrades_to_noop(monkeypatch, tmp_path):
    # /start succeeds, /stop exits nonzero and writes nothing -> we trust the absent file, not the code.
    monkeypatch.setattr(bi.SubProcess, 'run', staticmethod(lambda args, **k: 0 if '/start' in args else 5))
    with bi.VcPerfSession('vcperf.exe', str(tmp_path / 'nope.json')) as s:
        assert s.ok   # /start reported success
    assert not s.ok   # /stop produced no file -> no-op


def test_start_auto_runs_grantusercontrol_then_retries(monkeypatch, tmp_path):
    calls, out = [], tmp_path / 'tt.json'
    def _run(args, io_func=None, **k):
        calls.append(args); joined = ' '.join(args)
        if 'grantusercontrol' in joined: return 0   # elevated grant accepted
        if '/start' in args:
            if sum('/start' in c for c in calls) == 1:   # first start: not yet granted
                if io_func: io_func(None, 'requires /grantusercontrol with admin')
                return 1
            return 0                                     # retry after grant succeeds
        out.write_text('{}'); return 0                   # /stop writes the trace
    monkeypatch.setattr(bi.SubProcess, 'run', staticmethod(_run))
    monkeypatch.setattr(bi.System, 'windows', True)
    with bi.VcPerfSession('vcperf.exe', str(out)) as s:
        assert s.ok
    joined = [' '.join(c) for c in calls]
    assert any('grantusercontrol' in c for c in joined) and sum('/start' in c for c in joined) == 2


def test_grantusercontrol_hint_is_concise_when_grant_declined(monkeypatch, capsys):
    def _run(args, io_func=None, **k):
        if io_func: io_func(None, 'requires to run vcperf with the `/grantusercontrol` flag with admin')
        return 1   # every command (incl. the declined elevated grant) fails
    monkeypatch.setattr(bi.SubProcess, 'run', staticmethod(_run))
    monkeypatch.setattr(bi.System, 'windows', True)
    with bi.VcPerfSession('vcperf.exe', 'out.json') as s:
        pass
    out = strip_ansi(capsys.readouterr().out)
    assert not s.ok and '/grantusercontrol' in out and 'Failed to start trace' not in out  # the blurb is collapsed
