"""Pins BuildConfig: default jobs (container limit, Linux leaves a core free), the compiler-conflict note, flag aliases."""
import os, psutil, threading
import pytest
from mama.build_config import BuildConfig
from mama.utils import system


@pytest.fixture(autouse=True)
def _fresh_cpu_memo(monkeypatch):
    monkeypatch.setattr(system, '_cpu_count', 0)   # usable_cpu_count memoizes, so clear it per test


@pytest.fixture
def cpus(monkeypatch, tmp_path):
    """usable_cpu_count() on a Linux host of `host` cpus, with a cgroup tree under tmp_path."""
    def measure(*files, host=32, affinity=32, rel=''):
        monkeypatch.setattr(system, '_CGROUP_ROOT', tmp_path.as_posix())
        monkeypatch.setattr(system, '_cgroup_rel_paths', lambda: (rel, rel))
        monkeypatch.setattr(system, 'is_linux', True)
        monkeypatch.setattr(psutil, 'cpu_count', lambda: host)
        # raising=False: Windows has no sched_getaffinity, and these tests force the Linux branch
        monkeypatch.setattr(os, 'sched_getaffinity', lambda pid: set(range(affinity)), raising=False)
        for name, text in files:
            path = tmp_path / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        return system.usable_cpu_count()
    return measure


def test_a_cgroup_v2_quota_caps_the_cpu_count(cpus):
    assert cpus(('cpu.max', '300000 100000')) == 3          # --cpus=3


def test_a_cgroup_v1_quota_caps_the_cpu_count(cpus):
    # 1.5 cpus rounds UP: two compiles timeshare, one would leave the quota unused
    assert cpus(('cpu/cpu.cfs_quota_us', '150000'), ('cpu/cpu.cfs_period_us', '100000')) == 2


@pytest.mark.parametrize('files', [
    [('cpu.max', 'max 100000')],
    [('cpu/cpu.cfs_quota_us', '-1'), ('cpu/cpu.cfs_period_us', '100000')],
    [('cpu.max', 'garbage')],
    [],
], ids=['v2-max', 'v1-unlimited', 'unreadable', 'no-controller'])
def test_no_cgroup_limit_keeps_the_host_count(cpus, files):
    assert cpus(*files) == 32


def test_a_quota_in_the_process_cgroup_caps_the_cpu_count(cpus):
    # with no private cgroup namespace the mount root is an unlimited ancestor, not this process
    assert cpus(('cpu.max', 'max 100000'), ('svc/cpu.max', '200000 100000'), rel='svc') == 2


def test_the_smallest_quota_in_the_chain_wins(cpus):
    assert cpus(('svc/cpu.max', '400000 100000'), ('svc/app/cpu.max', '200000 100000'), rel='svc/app') == 2


def test_an_ancestor_quota_caps_a_child_that_sets_none(cpus):
    assert cpus(('svc/cpu.max', '400000 100000'), ('svc/app/cpu.max', 'max 100000'), rel='svc/app') == 4


@pytest.mark.parametrize('text, expect', [
    ('0::/svc/app\n', ('svc/app', '')),                                # v2 unified hierarchy
    ('4:cpu,cpuacct:/svc/app\n2:memory:/other\n', ('', 'svc/app')),    # v1, cpu grouped with cpuacct
    ('0::/unified\n4:cpu,cpuacct:/v1path\n', ('unified', 'v1path')),   # hybrid: each mount its own path
    ('0::/\n', ('', '')),                                              # a private cgroup namespace
    ('2:memory:/other\n', ('', '')),                                   # no cpu controller
], ids=['v2', 'v1', 'hybrid', 'namespaced', 'no-cpu-controller'])
def test_the_proc_cgroup_lines_name_each_mount_cgroup(monkeypatch, tmp_path, text, expect):
    proc = tmp_path / 'cgroup'
    proc.write_text(text)
    monkeypatch.setattr(system, '_PROC_CGROUP', proc.as_posix())
    assert system._cgroup_rel_paths() == expect


def test_a_hybrid_host_reads_the_v1_quota_under_the_v1_path(monkeypatch, tmp_path):
    # the unified path names no v1 cgroup, so appending it to the cpu mount misses the quota
    monkeypatch.setattr(system, '_cpu_count', 0)
    monkeypatch.setattr(system, '_CGROUP_ROOT', tmp_path.as_posix())
    monkeypatch.setattr(system, '_cgroup_rel_paths', lambda: ('unified', 'v1path'))
    monkeypatch.setattr(system, 'is_linux', True)
    monkeypatch.setattr(psutil, 'cpu_count', lambda: 32)
    monkeypatch.setattr(os, 'sched_getaffinity', lambda pid: set(range(32)), raising=False)
    (tmp_path / 'cpu' / 'v1path').mkdir(parents=True)
    (tmp_path / 'cpu' / 'v1path' / 'cpu.cfs_quota_us').write_text('300000')
    (tmp_path / 'cpu' / 'v1path' / 'cpu.cfs_period_us').write_text('100000')
    assert system.usable_cpu_count() == 3


def test_a_cpuset_affinity_mask_caps_the_cpu_count(cpus):
    assert cpus(affinity=2) == 2                            # --cpuset-cpus=0-1 writes no quota


def test_the_default_jobs_read_the_container_limit(cpus, monkeypatch):
    monkeypatch.setattr(system.System, 'linux', True)
    cpus(('cpu.max', '300000 100000'))
    assert BuildConfig._default_build_jobs() == 2           # 3 usable, minus the core Linux keeps free


def test_default_jobs_leaves_one_core_free_on_linux(cpus, monkeypatch):
    cpus()                                           # 32 cpus, no container limit
    monkeypatch.setattr(system.System, 'linux', True)
    assert BuildConfig._default_build_jobs() == 31   # N-1: do not saturate the box into an OOM/freeze
    monkeypatch.setattr(system.System, 'linux', False)
    assert BuildConfig._default_build_jobs() == 32   # Windows/macOS use all cores


def test_default_jobs_never_below_one(cpus, monkeypatch):
    cpus(host=1, affinity=1)
    monkeypatch.setattr(system.System, 'linux', True)
    assert BuildConfig._default_build_jobs() == 1


def _bare_cfg(**attrs):
    c = object.__new__(BuildConfig)  # skip the heavy __init__, set only what prefer_gcc touches
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
    # platform option builders run per fingerprint computation, so a plain console() repeats 'Toolchain: ...' per target
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
