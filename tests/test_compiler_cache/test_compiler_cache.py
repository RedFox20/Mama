"""Pins cmake_compiler_cache: fingerprint, publish/load/inject round-trip, TTL, purge, and the
primer-election Coordinator. inject reproduces cmake's warm state (PLATFORM_INFO_INITIALIZED marker
+ captured compiler files) so a fresh build dir skips ALL detection."""
import os, threading
from mama import cmake_compiler_cache as cc
from mama.util import normalized_path


def _fake_build_files(d, langs=('C', 'CXX', 'RC'), vs=True):
    """A `CMakeFiles/<ver>` dir as cmake would leave it after detection."""
    os.makedirs(d, exist_ok=True)
    open(os.path.join(d, 'CMakeSystem.cmake'), 'w').write('set(CMAKE_SYSTEM Windows)\n')
    for lang in langs:
        mod, abi = cc._LANG_FILES[lang]
        open(os.path.join(d, mod), 'w').write(f'set(CMAKE_{lang}_COMPILER "C:/bin/{lang.lower()}.exe")\n')
        if abi: open(os.path.join(d, abi), 'wb').write(b'\x00abi')
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
    # Regression: the seed must transplant ONLY compiler detection, never project flags/defines,
    # so a cache from project A can't poison project B. The injected cache is exactly two markers.
    bf = _fake_build_files(str(tmp_path / 'A' / '4.2.3'))
    seed = str(tmp_path / 'seed'); cc.publish(seed, bf)
    build = str(tmp_path / 'B'); bfd = os.path.join(build, 'CMakeFiles', '4.2.3')
    src = str(tmp_path / 'b')
    cc.inject(seed, build, bfd, src_dir=src)
    lines = [l.strip() for l in open(os.path.join(build, 'CMakeCache.txt')) if l.strip()]
    assert lines == ['CMAKE_PLATFORM_INFO_INITIALIZED:INTERNAL=1', f'CMAKE_HOME_DIRECTORY:INTERNAL={normalized_path(src)}']
    assert 'CMakeCache.txt' not in os.listdir(seed)  # we never capture a project's cache


def test_publish_returns_false_without_compiler_files(tmp_path):
    empty = str(tmp_path / 'x' / '4.2.3'); os.makedirs(empty)
    assert cc.publish(str(tmp_path / 'seed'), empty) is False


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


class _T:
    def __init__(self, build_dir, src='S:/src', with_files=False):
        self.build_dir = build_dir
        self.bfd = os.path.join(build_dir, 'CMakeFiles', '4.2.3')
        self.src = src
        if with_files: _fake_build_files(self.bfd)


def _coord(tmp_path, **kw):
    return cc.Coordinator(str(tmp_path / 'cache'), fp_fn=lambda t: 'FP',
                          paths_fn=lambda t: (t.build_dir, t.bfd, t.src), **kw)


def test_coordinator_first_primes_then_others_reuse(tmp_path):
    co = _coord(tmp_path)
    primer = _T(str(tmp_path / 'A'), with_files=True)
    assert co.prepare(primer) == 'prime'
    co.publish(primer)
    proj = str(tmp_path / 'proj'); consumer = _T(str(tmp_path / 'B'), src=proj)
    assert co.prepare(consumer) == 'use'
    assert os.path.exists(os.path.join(consumer.bfd, 'CMakeCXXCompiler.cmake'))   # seed injected
    cache = open(os.path.join(consumer.build_dir, 'CMakeCache.txt')).read()
    assert f'CMAKE_HOME_DIRECTORY:INTERNAL={normalized_path(proj)}' in cache       # B's own source, not A's


def test_coordinator_waiter_blocks_until_primer_publishes(tmp_path):
    co = _coord(tmp_path)
    assert co.prepare(_T(str(tmp_path / 'A'), with_files=True)) == 'prime'
    primer = _T(str(tmp_path / 'A'), with_files=True)
    out = {}
    w = threading.Thread(target=lambda: out.__setitem__('role', co.prepare(_T(str(tmp_path / 'B')))))
    w.start()
    w.join(0.2); assert w.is_alive()  # blocked: primer hasn't published yet
    co.publish(primer)
    w.join(3.0)
    assert out['role'] == 'use'


def test_coordinator_elects_exactly_one_primer_under_race(tmp_path):
    co = _coord(tmp_path, wait_timeout=5.0)
    n = 12
    targets = [_T(str(tmp_path / f't{i}'), with_files=True) for i in range(n)]
    barrier = threading.Barrier(n); roles = [None] * n
    def work(i):
        barrier.wait()
        role = co.prepare(targets[i])
        if role == 'prime': co.publish(targets[i])
        roles[i] = role
    ts = [threading.Thread(target=work, args=(i,)) for i in range(n)]
    for t in ts: t.start()
    for t in ts: t.join(8.0)
    assert roles.count('prime') == 1 and roles.count('use') == n - 1


def test_coordinator_failed_primer_lets_a_later_target_reelect(tmp_path):
    co = _coord(tmp_path)
    assert co.prepare(_T(str(tmp_path / 'A'), with_files=True)) == 'prime'
    co.fail_primer(_T(str(tmp_path / 'A')))           # primer configure failed, no seed published
    assert co.prepare(_T(str(tmp_path / 'B'))) == 'prime'  # re-elected, not stuck waiting forever


def test_coordinator_heal_purges_seed(tmp_path):
    co = _coord(tmp_path)
    primer = _T(str(tmp_path / 'A'), with_files=True)
    co.prepare(primer); co.publish(primer)
    assert cc.load(co.seed_dir(primer)) is not None
    co.heal(primer)
    assert cc.load(co.seed_dir(primer)) is None


def test_coordinator_disabled_is_noop(tmp_path):
    co = _coord(tmp_path, enabled=False)
    assert co.prepare(_T(str(tmp_path / 'x'))) == 'none'
