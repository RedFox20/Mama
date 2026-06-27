"""Pins cmake_compiler_cache: fingerprint stability, seed publish/load/inject round-trip, TTL, purge,
and the primer-election Coordinator."""
import os, threading
from mama import cmake_compiler_cache as cc


def _fake_build_files(d, langs=('C', 'CXX', 'RC'), vs=True):
    """A `CMakeFiles/<ver>` dir as cmake would leave it after detection."""
    os.makedirs(d, exist_ok=True)
    open(os.path.join(d, 'CMakeSystem.cmake'), 'w').write('set(CMAKE_SYSTEM Windows)\n')
    for lang in langs:
        mod, abi = cc._LANG_FILES[lang]
        open(os.path.join(d, mod), 'w').write(
            f'set(CMAKE_{lang}_COMPILER "C:/bin/{lang.lower()}.exe")\n'
            f'set(CMAKE_{lang}_COMPILER_ID "MSVC")\n'
            f'set(CMAKE_{lang}_COMPILER_VERSION "19.50")\n'
            f'set(CMAKE_{lang}_COMPILE_FEATURES "{lang.lower()}_std_11;{lang.lower()}_std_17")\n')
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


def test_seed_cmake_text_emits_compiler_abi_and_works():
    text = cc._seed_cmake_text(['CXX', 'RC'], {'CXX': {'CMAKE_CXX_COMPILER': '"c++"'}})
    assert 'set(CMAKE_CXX_COMPILER "c++" CACHE INTERNAL' in text
    assert 'set(CMAKE_CXX_ABI_COMPILED TRUE' in text and 'set(CMAKE_RC_COMPILER_WORKS 1' in text
    assert 'CMAKE_RC_ABI_COMPILED' not in text  # RC has no ABI step
    # compile-features is intentionally NOT seeded (fatal-errors in CMakeCommonCompilerMacros)
    assert 'COMPILE_FEATURES' not in text


def test_read_compiler_vars_extracts_only_the_compiler_path(tmp_path):
    bf = _fake_build_files(str(tmp_path / '4.2.3'), langs=('CXX',), vs=False)
    v = cc._read_compiler_vars(bf, 'CXX')
    assert v == {'CMAKE_CXX_COMPILER': '"C:/bin/cxx.exe"'}  # features/id deliberately excluded


def test_publish_then_inject_round_trip(tmp_path):
    bf = _fake_build_files(str(tmp_path / 'A' / '4.2.3'))
    seed = str(tmp_path / 'seed')
    assert cc.publish(seed, bf, clock=lambda: 1000)
    m = cc.load(seed, clock=lambda: 1000)
    assert m and set(m['langs']) == {'C', 'CXX', 'RC'} and m['cmake_files_ver'] == '4.2.3'

    fresh = str(tmp_path / 'B' / '4.2.3')          # a new package's empty build dir
    arg = cc.inject(seed, fresh)
    assert arg.startswith('-C ') and cc._SEED_CMAKE in arg
    for f in ('CMakeCXXCompiler.cmake', 'CMakeDetermineCompilerABI_CXX.bin', 'CMakeSystem.cmake', 'VCTargetsPath.txt'):
        assert os.path.exists(os.path.join(fresh, f))  # detection artifacts seeded into the fresh dir
    seed_cmake = open(os.path.join(seed, cc._SEED_CMAKE)).read()
    assert 'set(CMAKE_CXX_COMPILER "C:/bin/cxx.exe" CACHE INTERNAL' in seed_cmake  # detected vars captured


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
    def __init__(self, bfd): self.bfd = bfd


def _coord(tmp_path, **kw):
    root = str(tmp_path / 'cache')
    return cc.Coordinator(root, fp_fn=lambda t: 'FP', bfd_fn=lambda t: t.bfd, **kw)


def test_coordinator_first_primes_then_others_reuse(tmp_path):
    co = _coord(tmp_path)
    primer = _T(_fake_build_files(str(tmp_path / 'A' / '4.2.3')))
    assert co.prepare(primer) == ('', 'prime')
    co.publish(primer)
    consumer = _T(str(tmp_path / 'B' / '4.2.3'))
    arg, role = co.prepare(consumer)
    assert role == 'use' and arg.startswith('-C ')
    assert os.path.exists(os.path.join(consumer.bfd, 'CMakeCXXCompiler.cmake'))


def test_coordinator_waiter_blocks_until_primer_publishes(tmp_path):
    co = _coord(tmp_path)
    primer = _T(_fake_build_files(str(tmp_path / 'A' / '4.2.3')))
    assert co.prepare(primer)[1] == 'prime'
    out = {}
    w = threading.Thread(target=lambda: out.update(zip(['arg', 'role'], co.prepare(_T(str(tmp_path / 'B' / '4.2.3'))))))
    w.start()
    w.join(0.2); assert w.is_alive()  # blocked: primer hasn't published yet
    co.publish(primer)
    w.join(3.0)
    assert out['role'] == 'use'


def test_coordinator_elects_exactly_one_primer_under_race(tmp_path):
    co = _coord(tmp_path, wait_timeout=5.0)
    n = 12
    targets = [_T(_fake_build_files(str(tmp_path / f't{i}' / '4.2.3'))) for i in range(n)]
    barrier = threading.Barrier(n); roles = [None] * n
    def work(i):
        barrier.wait()
        _, role = co.prepare(targets[i])
        if role == 'prime': co.publish(targets[i])
        roles[i] = role
    ts = [threading.Thread(target=work, args=(i,)) for i in range(n)]
    for t in ts: t.start()
    for t in ts: t.join(8.0)
    assert roles.count('prime') == 1 and roles.count('use') == n - 1


def test_coordinator_failed_primer_lets_a_later_target_reelect(tmp_path):
    co = _coord(tmp_path)
    t1 = _T(_fake_build_files(str(tmp_path / 'A' / '4.2.3')))
    assert co.prepare(t1)[1] == 'prime'
    co.fail_primer(t1)                       # primer configure failed, no seed published
    t2 = _T(_fake_build_files(str(tmp_path / 'B' / '4.2.3')))
    assert co.prepare(t2)[1] == 'prime'      # re-elected, not stuck waiting forever


def test_coordinator_heal_purges_seed(tmp_path):
    co = _coord(tmp_path)
    primer = _T(_fake_build_files(str(tmp_path / 'A' / '4.2.3')))
    co.prepare(primer); co.publish(primer)
    assert cc.load(co.seed_dir(primer)) is not None
    co.heal(primer)
    assert cc.load(co.seed_dir(primer)) is None


def test_coordinator_disabled_is_noop(tmp_path):
    co = _coord(tmp_path, enabled=False)
    assert co.prepare(_T(str(tmp_path / 'x'))) == ('', 'none')
