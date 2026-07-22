"""Pins cmake_compiler_cache: fingerprint, publish/load/inject round-trip, TTL, purge, and the
probe-seeded Coordinator."""
import contextlib, os, threading, time
from types import SimpleNamespace
from mama import cmake_compiler_cache as cc
from mama.util import normalized_path, path_join


def _fake_build_files(d, langs=('C', 'CXX', 'RC'), vs=True, partial=()):
    """A `CMakeFiles/<ver>` dir as cmake leaves it post-detection; `partial` langs stop at stage 1 (no ABI
    probe), as a killed configure leaves them."""
    os.makedirs(d, exist_ok=True)
    open(os.path.join(d, 'CMakeSystem.cmake'), 'w').write('set(CMAKE_SYSTEM Windows)\n')
    for lang in langs:
        mod, abi = cc._LANG_FILES[lang]
        done = lang not in partial
        text = f'set(CMAKE_{lang}_COMPILER "C:/bin/{lang.lower()}.exe")\n'
        if abi and done: text += f'set(CMAKE_{lang}_ABI_COMPILED TRUE)\n'
        open(os.path.join(d, mod), 'w').write(text)
        if abi and done: open(os.path.join(d, abi), 'wb').write(b'\x00abi')
    if vs: open(os.path.join(d, 'VCTargetsPath.txt'), 'w').write('C:/VCTargets\n')
    return d


def test_fingerprint_stable_and_sensitive():
    a = {'cmake': '4.2.3', 'gen': 'VS18', 'cc': {'mtime': 1}}
    assert cc.compute_fingerprint(a) == cc.compute_fingerprint(dict(a))      # order-independent, stable
    assert cc.compute_fingerprint(a) != cc.compute_fingerprint({**a, 'cmake': '4.3.0'})
    assert cc.compute_fingerprint(a) != cc.compute_fingerprint({**a, 'cc': {'mtime': 2}})  # compiler updated


def test_compiler_stat_reports_size_mtime_or_bare_path(tmp_path):
    f = tmp_path / 'cl.exe'; f.write_bytes(b'x' * 10)
    s = cc.compiler_stat(str(f))
    assert s['size'] == 10 and 'mtime' in s and s['path'].endswith('cl.exe')
    assert cc.compiler_stat(str(tmp_path / 'nope')) == {'path': str(tmp_path / 'nope')}


def test_detected_langs_from_files(tmp_path):
    d = _fake_build_files(str(tmp_path / '4.2.3'), langs=('CXX',), vs=False)
    assert cc.detected_langs(d) == ['CXX']


def test_publish_then_inject_reproduces_warm_state(tmp_path):
    bf = _fake_build_files(str(tmp_path / 'A' / '4.2.3'))
    seed = str(tmp_path / 'seed')
    assert cc.publish(seed, bf, clock=lambda: 1000)
    m = cc.load(seed, clock=lambda: 1000)
    assert m and set(m['langs']) == {'C', 'CXX', 'RC'} and m['cmake_files_ver'] == '4.2.3'

    build = str(tmp_path / 'B'); bfd = os.path.join(build, 'CMakeFiles', '4.2.3')
    src = str(tmp_path / 'proj_src')
    cc.inject(seed, build, bfd, src_dir=src)
    for f in ('CMakeCXXCompiler.cmake', 'CMakeDetermineCompilerABI_CXX.bin', 'CMakeSystem.cmake', 'VCTargetsPath.txt'):
        assert os.path.exists(os.path.join(bfd, f))     # toolchain files copied into CMakeFiles/<ver>
    cache = open(os.path.join(build, 'CMakeCache.txt')).read()
    assert 'CMAKE_PLATFORM_INFO_INITIALIZED:INTERNAL=1' in cache       # the marker that skips detection
    assert f'CMAKE_HOME_DIRECTORY:INTERNAL={normalized_path(src)}' in cache  # must match the configured source


def test_inject_writes_only_toolchain_markers_never_project_settings(tmp_path):
    # Regression: injected cache is toolchain markers only (no ABI facts captured here), no project flags.
    bf = _fake_build_files(str(tmp_path / 'A' / '4.2.3'))
    seed = str(tmp_path / 'seed'); cc.publish(seed, bf)
    build = str(tmp_path / 'B'); bfd = os.path.join(build, 'CMakeFiles', '4.2.3')
    src = str(tmp_path / 'b')
    cc.inject(seed, build, bfd, src_dir=src)
    lines = [l.strip() for l in open(os.path.join(build, 'CMakeCache.txt')) if l.strip()]
    assert lines == ['CMAKE_PLATFORM_INFO_INITIALIZED:INTERNAL=1', f'CMAKE_HOME_DIRECTORY:INTERNAL={normalized_path(src)}']
    assert 'CMakeCache.txt' not in os.listdir(seed)  # we never capture a project's cache


def _write_cache(build_dir, extra=''):
    os.makedirs(build_dir, exist_ok=True)
    text = 'CMAKE_GENERATOR:INTERNAL=Ninja\nCMAKE_EXECUTABLE_FORMAT:INTERNAL=ELF\nBUILD_TESTS:BOOL=ON\n' + extra
    open(os.path.join(build_dir, 'CMakeCache.txt'), 'w').write(text)


def test_seeded_cache_replays_the_abi_facts_the_probe_would_have_set(tmp_path):
    # no CMAKE_EXECUTABLE_FORMAT -> seeded configure dies on every install-RPATH add_executable
    build = str(tmp_path / 'A')
    bf = _fake_build_files(os.path.join(build, 'CMakeFiles', '4.2.3'))
    _write_cache(build, 'CMAKE_LIBRARY_ARCHITECTURE:INTERNAL=x86_64-linux-gnu\n')
    seed = str(tmp_path / 'seed')
    assert cc.publish(seed, bf, build_dir=build)

    dst = str(tmp_path / 'B')
    cc.inject(seed, dst, os.path.join(dst, 'CMakeFiles', '4.2.3'), src_dir=str(tmp_path / 'src'))
    cache = open(os.path.join(dst, 'CMakeCache.txt')).read()
    assert 'CMAKE_EXECUTABLE_FORMAT:INTERNAL=ELF' in cache
    assert 'CMAKE_LIBRARY_ARCHITECTURE:INTERNAL=x86_64-linux-gnu' in cache
    assert 'BUILD_TESTS' not in cache  # toolchain facts only, no project settings leak


def test_publish_returns_false_without_compiler_files(tmp_path):
    empty = str(tmp_path / 'x' / '4.2.3'); os.makedirs(empty)
    assert cc.publish(str(tmp_path / 'seed'), empty) is False


def test_detection_is_partial_spots_a_killed_detection(tmp_path):
    assert not cc.detection_is_partial(_fake_build_files(str(tmp_path / 'ok' / '4.2.3')))
    assert cc.detection_is_partial(_fake_build_files(str(tmp_path / 'bad' / '4.2.3'), partial=('CXX',)))
    # RC has no ABI probe: a module without one is complete, not partial
    assert not cc.detection_is_partial(_fake_build_files(str(tmp_path / 'rc' / '4.2.3'), langs=('RC',)))


def test_publish_refuses_a_half_detected_toolchain(tmp_path):
    # else one interrupted configure poisons every project via the shared seed
    bf = _fake_build_files(str(tmp_path / 'A' / '4.2.3'), partial=('CXX',))
    seed = str(tmp_path / 'seed')
    assert cc.publish(seed, bf) is False
    assert cc.load(seed) is None


def test_publish_refuses_a_seed_missing_a_core_language(tmp_path):
    # backstop for the seeding invariant: a seed missing a core language would let a project that
    # enables it skip detection and die on 'CMAKE_<lang>_COMPILER not set, after EnableLanguage'.
    for langs in (('C',), ('CXX',), ('C', 'RC')):
        bf = _fake_build_files(str(tmp_path / '_'.join(langs) / '4.2.3'), langs=langs)
        seed = str(tmp_path / ('seed_' + '_'.join(langs)))
        assert cc.publish(seed, bf) is False, f'{langs} must not publish'
        assert cc.load(seed) is None
    # both core languages present -> publishable
    bf = _fake_build_files(str(tmp_path / 'full' / '4.2.3'), langs=('C', 'CXX'))
    assert cc.publish(str(tmp_path / 'seed_full'), bf) is True


def test_is_valid_rejects_a_single_language_seed(tmp_path):
    # _try_use purges what is_valid rejects, so an on-disk C-only seed self-heals instead of poisoning
    assert not cc.is_valid({'fingerprint': 'FP', 'langs': ['C']}, 'FP')
    assert not cc.is_valid({'fingerprint': 'FP', 'langs': ['CXX']}, 'FP')
    assert not cc.is_valid({'fingerprint': 'FP', 'langs': []}, 'FP')
    assert cc.is_valid({'fingerprint': 'FP', 'langs': ['C', 'CXX']}, 'FP')
    assert not cc.is_valid({'fingerprint': 'FP'}, 'FP')  # no langs record -> can't prove it covers C+CXX


def test_inject_writes_no_marker_when_seed_has_no_files(tmp_path):
    # A vanished/empty seed must NOT leave a PLATFORM_INFO marker with zero compiler files - cmake
    # would then trust detection that isn't there. inject bails (False); the caller redetects.
    seed = str(tmp_path / 'seed'); os.makedirs(seed)
    open(os.path.join(seed, cc._MANIFEST), 'w').write('{"files": [], "langs": []}')
    build = str(tmp_path / 'B')
    assert cc.inject(seed, build, os.path.join(build, 'CMakeFiles', '4.2.3'), 'S:/src') is False
    assert not os.path.exists(os.path.join(build, 'CMakeCache.txt'))


def test_load_honors_backstop_ttl(tmp_path):
    bf = _fake_build_files(str(tmp_path / 'A' / '4.2.3'))
    seed = str(tmp_path / 'seed')
    cc.publish(seed, bf, clock=lambda: 1000)
    assert cc.load(seed, ttl=100, clock=lambda: 1050) is not None  # within TTL
    assert cc.load(seed, ttl=100, clock=lambda: 2000) is None      # past TTL


def test_purge_removes_seed(tmp_path):
    bf = _fake_build_files(str(tmp_path / 'A' / '4.2.3'))
    seed = str(tmp_path / 'seed')
    cc.publish(seed, bf)
    cc.purge(seed)
    assert cc.load(seed) is None
    cc.purge(seed)  # idempotent, never raises


def test_is_valid_requires_matching_fingerprint_and_live_probe(tmp_path):
    cl = tmp_path / 'cl.exe'; cl.write_text('')
    m = {'fingerprint': 'FP', 'probe': str(cl), 'langs': ['C', 'CXX']}
    assert cc.is_valid(m, 'FP')
    assert not cc.is_valid(m, 'OTHER')                                        # fingerprint changed
    assert not cc.is_valid({'fingerprint': 'FP', 'probe': str(tmp_path / 'gone.exe'),
                            'langs': ['C', 'CXX']}, 'FP')  # compiler removed
    assert not cc.is_valid(None, 'FP') and not cc.is_valid({}, 'FP')
    assert cc.is_valid({'fingerprint': 'FP', 'langs': ['C', 'CXX']}, 'FP')    # no probe -> fingerprint alone gates


def test_publish_records_fingerprint_and_probe_so_a_fresh_seed_validates(tmp_path):
    bf = _fake_build_files(str(tmp_path / 'A' / '4.2.3'))
    cl = tmp_path / 'cl.exe'; cl.write_text('')
    seed = str(tmp_path / 'seed')
    cc.publish(seed, bf, fingerprint='FP', probe=str(cl))
    m = cc.load(seed)
    assert m['fingerprint'] == 'FP' and m['probe'] == str(cl) and cc.is_valid(m, 'FP')


def test_gc_stale_drops_legacy_and_dead_probe_but_keeps_live(tmp_path):
    root = str(tmp_path / 'cache')
    cl = tmp_path / 'cl.exe'; cl.write_text('')
    bf = _fake_build_files(str(tmp_path / 'bf' / '4.2.3'))
    cc.publish(path_join(root, 'live'), bf, fingerprint='L', probe=str(cl))            # toolchain present
    cc.publish(path_join(root, 'dead'), bf, fingerprint='D', probe=str(tmp_path / 'x'))  # compiler gone
    legacy = path_join(root, 'legacy'); os.makedirs(legacy)
    open(path_join(legacy, cc._MANIFEST), 'w').write('{"files": [], "langs": [], "created": 0}')
    cc.gc_stale(root)
    assert set(os.listdir(root)) == {'live'}   # dead-probe and legacy (no fingerprint) seeds purged


def test_begin_session_sweeps_and_logs_once(tmp_path):
    root = str(tmp_path / 'cache')
    legacy = path_join(root, 'old'); os.makedirs(legacy)
    open(path_join(legacy, cc._MANIFEST), 'w').write('{"files": [], "langs": []}')   # no fingerprint -> stale
    logs = []
    co = cc.Coordinator(root, fp_fn=lambda t: 'FP', paths_fn=lambda t: None, log_fn=logs.append)
    co.begin_session(); co.begin_session()                          # second call is a no-op
    assert not os.path.exists(legacy)                               # legacy seed swept
    assert sum('compiler-seed cache:' in m for m in logs) == 1      # root logged exactly once
    assert any('drop stale seed old' in m for m in logs)


def test_begin_session_announces_disabled_cache(tmp_path):
    logs = []
    cc.Coordinator(str(tmp_path / 'c'), fp_fn=lambda t: 'FP', paths_fn=lambda t: None,
                   enabled=False, log_fn=logs.append).begin_session()
    assert any('disabled' in m for m in logs)


class _T:
    def __init__(self, build_dir, src='S:/src'):
        self.build_dir = build_dir
        self.bfd = os.path.join(build_dir, 'CMakeFiles', '4.2.3')
        self.src = src


def _probe(tmp_path, langs=('C', 'CXX'), calls=None, fail=False, delay=0.0):
    """Stand-in for the synthetic `project(mama_seed C CXX)` configure: yields a finished probe dir."""
    runs = []
    @contextlib.contextmanager
    def run(target):
        runs.append(target)
        if calls is not None: calls.append(target)
        if delay: time.sleep(delay)
        if fail: yield None; return
        d = str(tmp_path / f'probe{len(runs)}')
        bfd = _fake_build_files(os.path.join(d, 'CMakeFiles', '4.2.3'), langs=langs, vs=False)
        _write_cache(d)
        yield (d, bfd)
    return run


def _coord(tmp_path, **kw):
    kw.setdefault('seed_fn', _probe(tmp_path))
    return cc.Coordinator(str(tmp_path / 'cache'), fp_fn=lambda t: 'FP',
                          paths_fn=lambda t: (t.build_dir, t.bfd, t.src), **kw)


def test_probe_seeds_the_first_caller_and_every_later_one(tmp_path):
    calls = []
    co = _coord(tmp_path, seed_fn=_probe(tmp_path, calls=calls))
    assert co.prepare(_T(str(tmp_path / 'A'))) == 'use'        # the caller that ran the probe is seeded too
    proj = str(tmp_path / 'proj')
    assert co.prepare(_T(str(tmp_path / 'B'), src=proj)) == 'use'
    assert len(calls) == 1                                     # probe ran once for the fingerprint
    assert os.path.exists(os.path.join(str(tmp_path / 'B'), 'CMakeFiles', '4.2.3', 'CMakeCXXCompiler.cmake'))
    cache = open(os.path.join(str(tmp_path / 'B'), 'CMakeCache.txt')).read()
    assert f'CMAKE_HOME_DIRECTORY:INTERNAL={normalized_path(proj)}' in cache   # B's own source, not the probe's


def test_a_cxx_only_project_is_served_by_the_probe(tmp_path):
    # the bug this design fixes: a C-only or CXX-only project used to decide the seed's languages
    co = _coord(tmp_path)
    assert co.prepare(_T(str(tmp_path / 'cxx_only'))) == 'use'
    assert cc.load(co.seed_dir(_T(str(tmp_path / 'x'))))['langs'] == ['C', 'CXX']


def test_a_probe_missing_a_core_language_is_never_published(tmp_path):
    for i, langs in enumerate((('C',), ('CXX',))):
        sub = tmp_path / f'case{i}'
        co = _coord(sub, seed_fn=_probe(sub, langs=langs))
        assert co.prepare(_T(str(tmp_path / 'A'))) == 'none'    # detect normally rather than inject a gap
        assert cc.load(co.seed_dir(_T(str(tmp_path / 'A')))) is None


def test_waiter_blocks_until_the_probe_finishes(tmp_path):
    co = _coord(tmp_path, seed_fn=_probe(tmp_path, delay=0.4))
    out = {}
    first = threading.Thread(target=lambda: co.prepare(_T(str(tmp_path / 'A'))))
    first.start(); time.sleep(0.05)
    w = threading.Thread(target=lambda: out.__setitem__('role', co.prepare(_T(str(tmp_path / 'B')))))
    w.start(); w.join(0.1); assert w.is_alive()   # blocked while the probe runs
    first.join(3.0); w.join(3.0)
    assert out['role'] == 'use'


def test_probe_runs_exactly_once_under_race(tmp_path):
    calls = []
    co = _coord(tmp_path, wait_timeout=5.0, seed_fn=_probe(tmp_path, calls=calls, delay=0.05))
    n = 12
    targets = [_T(str(tmp_path / f't{i}')) for i in range(n)]
    barrier = threading.Barrier(n); roles = [None] * n
    def work(i):
        barrier.wait()
        roles[i] = co.prepare(targets[i])
    ts = [threading.Thread(target=work, args=(i,)) for i in range(n)]
    for t in ts: t.start()
    for t in ts: t.join(8.0)
    assert len(calls) == 1 and roles.count('use') == n


def test_failed_probe_lets_a_later_target_retry(tmp_path):
    calls = []
    co = _coord(tmp_path, seed_fn=_probe(tmp_path, calls=calls, fail=True))
    assert co.prepare(_T(str(tmp_path / 'A'))) == 'none'
    assert co.prepare(_T(str(tmp_path / 'B'))) == 'none'
    assert len(calls) == 2       # re-elected, not stuck waiting on a dead fingerprint forever


def test_a_raising_probe_wakes_waiters(tmp_path):
    # a probe that throws (disk full) must release blocked waiters, not hang them for wait_timeout
    @contextlib.contextmanager
    def boom(target): raise OSError('disk full')
    co = _coord(tmp_path, seed_fn=boom)
    out = {}
    w = threading.Thread(target=lambda: out.__setitem__('role', co.prepare(_T(str(tmp_path / 'B')))))
    w.start(); w.join(3.0)
    assert not w.is_alive() and out['role'] == 'none'


def test_status_reports_fingerprint_and_presence(tmp_path):
    co = _coord(tmp_path)
    a = _T(str(tmp_path / 'A'))
    assert co.status(a) == ('FP', False)        # nothing published yet
    co.prepare(a)
    assert co.status(a) == ('FP', True)


def test_new_fingerprint_reruns_the_probe(tmp_path):
    fp = {'v': 'FP1'}; calls = []
    co = cc.Coordinator(str(tmp_path / 'cache'), fp_fn=lambda t: fp['v'], seed_fn=_probe(tmp_path, calls=calls),
                        paths_fn=lambda t: (t.build_dir, t.bfd, t.src))
    assert co.prepare(_T(str(tmp_path / 'A'))) == 'use'
    fp['v'] = 'FP2'                                    # toolchain (or stdlib) changed
    assert co.prepare(_T(str(tmp_path / 'B'))) == 'use'
    assert len(calls) == 2                             # the old seed is not reused across fingerprints


def test_a_dead_compiler_invalidates_the_seed(tmp_path):
    cl = tmp_path / 'cl.exe'; cl.write_text('')
    calls = []
    co = cc.Coordinator(str(tmp_path / 'cache'), fp_fn=lambda t: 'FP', probe_fn=lambda t: str(cl),
                        seed_fn=_probe(tmp_path, calls=calls),
                        paths_fn=lambda t: (t.build_dir, t.bfd, t.src))
    assert co.prepare(_T(str(tmp_path / 'A'))) == 'use'
    assert co.prepare(_T(str(tmp_path / 'B'))) == 'use' and len(calls) == 1   # probe alive -> reuse
    cl.unlink()                                                              # toolset removed
    assert co.prepare(_T(str(tmp_path / 'C'))) == 'none'                     # never hand out a dead seed
    assert cc.load(co.seed_dir(_T(str(tmp_path / 'C')))) is None             # ...and purge it


def test_heal_purges_seed(tmp_path):
    co = _coord(tmp_path)
    a = _T(str(tmp_path / 'A'))
    co.prepare(a)
    assert cc.load(co.seed_dir(a)) is not None
    co.heal(a)
    assert cc.load(co.seed_dir(a)) is None


def test_disabled_is_noop(tmp_path):
    calls = []
    co = _coord(tmp_path, enabled=False, seed_fn=_probe(tmp_path, calls=calls))
    assert co.prepare(_T(str(tmp_path / 'x'))) == 'none' and not calls


def test_reprobes_when_seed_files_vanished(tmp_path):
    # A concurrent heal can remove the toolchain files while the manifest lingers; prepare must never
    # return a doomed 'use' - with a probe it just rebuilds the seed.
    calls = []
    co = _coord(tmp_path, seed_fn=_probe(tmp_path, calls=calls))
    a = _T(str(tmp_path / 'A'))
    co.prepare(a)
    sd = co.seed_dir(a)
    for f in os.listdir(sd):
        if f != cc._MANIFEST: os.remove(os.path.join(sd, f))   # files gone, manifest stays
    assert co.prepare(_T(str(tmp_path / 'B'))) == 'use' and len(calls) == 2


def test_abi_flags_reach_the_probe_and_the_fingerprint():
    # the probe must detect with the same ABI inputs the real targets use, or its implicit link libs
    # (libc++ vs libstdc++, a sanitizer runtime) describe a toolchain nobody is actually building with
    from mama.cmake_configure import _abi_flags
    cfg = SimpleNamespace(linux=True, clang=True, msvc=False, clang_stdlib='libstdc++', sanitize=None)
    assert _abi_flags(cfg) == ('', '-stdlib=libstdc++')        # -stdlib is C++-only: clang warns on C
    cfg.sanitize = 'address'
    assert _abi_flags(cfg) == ('-fsanitize=address', '-fsanitize=address -stdlib=libstdc++')
    assert _abi_flags(SimpleNamespace(linux=True, clang=False, msvc=False, clang_stdlib='libc++',
                                      sanitize=None)) == ('', '')   # gcc: stdlib isn't a choice


def test_probe_cmd_carries_the_cross_toolchain(tmp_path, monkeypatch):
    # Yocto/raspi inject the sysroot + cross binutils via the platform opts, NOT via the obvious
    # CMAKE_*_COMPILER keys. A probe without them detects the HOST toolchain and publishes it for a
    # cross fingerprint - every seeded cross target then links against host libs.
    from mama import cmake_configure as cfg
    platform = ['CMAKE_SYSROOT=/opt/sdk/sysroot', 'CMAKE_SYSTEM_NAME=Linux', 'CMAKE_AR=/opt/sdk/bin/aarch64-ar']
    monkeypatch.setattr(cfg, '_platform_opts', lambda t: list(platform))
    monkeypatch.setattr(cfg, '_set_compiler_paths', lambda t, o: o.append('CMAKE_C_COMPILER=/opt/sdk/bin/aarch64-gcc'))
    monkeypatch.setattr(cfg, '_generator', lambda t: '-G "Ninja"')
    monkeypatch.setattr(cfg, '_cmake_version_number', lambda c: '4.2.3')
    seen = {}
    def fake_run(cmd, cwd, env=None, io_func=None): seen['cmd'] = cmd; return 1  # fail: we only want the cmd
    monkeypatch.setattr(cfg.SubProcess, 'run', staticmethod(fake_run))
    target = SimpleNamespace(cmake_command='cmake', config=SimpleNamespace(
        verbose=False, linux=True, clang=False, msvc=False, clang_stdlib='libc++', sanitize=None))
    monkeypatch.setattr(cfg, 'compute_env', lambda t: {})
    with cfg._probe_toolchain(target) as paths:
        assert paths is None
    for opt in platform + ['CMAKE_C_COMPILER=/opt/sdk/bin/aarch64-gcc']:
        assert f'-D{opt}' in seen['cmd'], f'probe dropped {opt}'


def test_an_sdk_move_changes_the_fingerprint(tmp_path, monkeypatch):
    # Yocto SDKs keep the compiler path stable across upgrades but move the sysroot; if that doesn't
    # reach the fingerprint, a cross build reuses a seed detected against the previous sysroot.
    from mama import cmake_configure as cfg
    opts = ['CMAKE_SYSTEM_NAME=Linux', 'CMAKE_SYSROOT=/opt/sdk-1.0/sysroot']
    monkeypatch.setattr(cfg, '_platform_opts', lambda t: list(opts))
    target = SimpleNamespace(cmake_opts=[])
    before = cc.compute_fingerprint(cfg._toolchain_inputs(target))
    opts[1] = 'CMAKE_SYSROOT=/opt/sdk-2.0/sysroot'
    assert cc.compute_fingerprint(cfg._toolchain_inputs(target)) != before
