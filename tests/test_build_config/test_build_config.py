"""Pins BuildConfig: default jobs (Linux leaves a core free), the once-only compiler-conflict note, flag aliases."""
import psutil, threading
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
    for name in ('myapp', 'netlib', 'ReCpp'): c.prefer_gcc(name)   # every dep re-requests GCC
    assert len(printed) == 1                                             # one note, not one per dep
    assert 'myapp requested GCC but compiler already set to Clang' in printed[0]


def test_buildstats_flag_enables_the_timing_report():
    c = object.__new__(BuildConfig)  # parse_args touches nothing else for these flags
    c.buildstats = False
    c.parse_args(['buildstats'])
    assert c.buildstats


def test_the_retired_buildtimes_flag_is_no_longer_recognized():
    c = object.__new__(BuildConfig)
    c.buildstats = False; c.unused_args = []
    c.parse_args(['buildtimes'])
    assert not c.buildstats and c.unused_args == ['buildtimes']  # falls through as an unknown arg


def test_announce_once_prints_a_key_only_the_first_time(monkeypatch):
    # platform option builders run per fingerprint computation, not per configure - a plain console()
    # repeats 'Toolchain: ...' several times per target with nothing new to say
    printed = []
    monkeypatch.setattr('mama.build_config.console', lambda t, **k: printed.append(t))
    c = object.__new__(BuildConfig)
    c.print = True; c._announced = set(); c._announce_lock = threading.Lock()
    for _ in range(3): c.announce_once('toolchain', 'Toolchain: /opt/sdk/arm.cmake')
    c.announce_once('other', 'MIPS Toolchain: /opt/mips.cmake')
    assert printed == ['Toolchain: /opt/sdk/arm.cmake', 'MIPS Toolchain: /opt/mips.cmake']


def test_announce_once_is_silent_when_printing_is_off():
    c = object.__new__(BuildConfig)
    c.print = False; c._announced = set(); c._announce_lock = threading.Lock()
    c.announce_once('toolchain', 'nope')
    assert c._announced == set()
