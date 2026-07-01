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
