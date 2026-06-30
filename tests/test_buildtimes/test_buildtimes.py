"""Pins the buildtimes report: normalized segmented bars, slowest-first, encoding-safe glyphs."""
import re, contextlib
from types import SimpleNamespace
from mama import dependency_chain as dc

def _strip(s): return re.sub(r'\x1b\[[0-9;]*m', '', s)
def _dep(name, **pt): return SimpleNamespace(name=name, phase_times=pt)


def test_bar_normalizes_to_slowest_and_pads_to_full_width():
    full = _strip(dc._buildtimes_bar({'build': 100.0}, 100.0, 100.0, dc._GLYPHS_ASCII))
    half = _strip(dc._buildtimes_bar({'build': 50.0}, 50.0, 100.0, dc._GLYPHS_ASCII))
    assert len(full) == len(half) == dc._BAR_FILL    # both padded to one fixed width -> totals align
    assert full.count('#') == dc._BAR_FILL           # the slowest dep fills the whole bar
    assert half.count('#') == round(dc._BAR_FILL / 2)  # half the time -> half the filled length


def test_bar_segments_are_proportional_in_load_cfg_build_order():
    bar = _strip(dc._buildtimes_bar({'load': 1.0, 'configure': 1.0, 'build': 2.0}, 4.0, 4.0, dc._GLYPHS_ASCII))
    assert bar == '-' * 10 + '=' * 10 + '#' * 20   # 25/25/50% of 40, ordered load(-) cfg(=) build(#), no gaps


def test_blocks_fall_back_to_ascii_on_a_legacy_code_page():
    assert not dc._can_encode_blocks('cp1252')   # Windows legacy code page can't encode ░▒▓ -> ASCII
    assert dc._can_encode_blocks('utf-8')


def test_bar_glyphs_is_computed_once_and_cached():
    assert dc._bar_glyphs() is dc._bar_glyphs()   # same object -> not recomputed per call
    assert dc._bar_glyphs() in (dc._GLYPHS_SHADE, dc._GLYPHS_ASCII)


def test_report_sorts_slowest_first_and_omits_pure_noops(capsys):
    deps = [_dep('fast', build=2.0), _dep('slow', load=1.0, configure=5.0, build=60.0), _dep('cached')]
    dc.print_buildtimes(deps)
    out = _strip(capsys.readouterr().out)
    assert 'cached' not in out                     # a dep with no timed phase is omitted
    assert out.index('slow') < out.index('fast')   # slowest package first
    assert '1m 6s' in out and '2.0s' in out        # totals via the shared get_time_str


def test_run_phase_accumulates_phase_time(monkeypatch):
    monkeypatch.setattr(dc.system, 'capture_to', lambda *a, **k: contextlib.nullcontext())
    disp = SimpleNamespace(start_task=lambda *a: None, feed=lambda *a: None,
                           finish_task=lambda *a: None, relabel=lambda *a: None)
    dep = SimpleNamespace(name='x', config=SimpleNamespace(verbose=False), phase_times={},
                          load_action='check', get_children=lambda: [], is_root=False)
    dc._run_phase(disp, dep, 'build', lambda s: None, None, final=True)
    assert 'build' in dep.phase_times and dep.phase_times['build'] >= 0
