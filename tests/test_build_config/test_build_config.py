"""Pins BuildConfig.jobs default: Linux leaves one core free for the desktop; other platforms use all."""
import psutil
from mama.build_config import BuildConfig
from mama.utils import system


def test_default_jobs_leaves_one_core_free_on_linux(monkeypatch):
    monkeypatch.setattr(psutil, 'cpu_count', lambda: 32)
    monkeypatch.setattr(system.System, 'linux', True)
    assert BuildConfig._default_build_jobs() == 31   # N-1: don't saturate the box into an OOM/freeze
    monkeypatch.setattr(system.System, 'linux', False)
    assert BuildConfig._default_build_jobs() == 32   # Windows/macOS use all cores


def test_default_jobs_never_below_one(monkeypatch):
    monkeypatch.setattr(psutil, 'cpu_count', lambda: 1)
    monkeypatch.setattr(system.System, 'linux', True)
    assert BuildConfig._default_build_jobs() == 1


def _bare_cfg(**attrs):
    c = object.__new__(BuildConfig)  # skip the heavy __init__; set only what prefer_gcc touches
    c.linux = True; c.raspi = False; c.gcc = False; c.clang = True
    c.compiler_cmd = True; c.print = True; c.compiler_conflict_warned = False
    for k, v in attrs.items(): setattr(c, k, v)
    return c


def test_compiler_conflict_note_fires_once_across_deps(monkeypatch):
    printed = []
    monkeypatch.setattr('mama.build_config.console', lambda t, **k: printed.append(t))
    c = _bare_cfg()  # compiler locked to Clang
    for name in ('krattcam', 'krattlink', 'ReCpp'): c.prefer_gcc(name)   # every dep re-requests GCC
    assert len(printed) == 1                                             # one note, not one per dep
    assert 'krattcam requested GCC but compiler already set to Clang' in printed[0]
