"""Pins _get_hierarchical_libs: a shared dep contributes its libs ONCE (not once per path through the
graph) and keeps Unix link order - every lib appears after everything that references it."""
import shutil, subprocess
from types import SimpleNamespace

import pytest

from mama import dependency_chain as dc
from mama.platforms.linux import Linux


def _dep(name, libs=(), syslibs=(), children=(), platform_class=Linux):
    config = SimpleNamespace(arch='x64')
    config.platform = platform_class(config)
    target = SimpleNamespace(exported_libs=list(libs), exported_syslibs=list(syslibs), config=config)
    return SimpleNamespace(name=name, target=target, get_children=lambda: list(children))


def test_diamond_dependency_contributes_its_libs_once():
    c = _dep('C', libs=['libC.a'])
    a = _dep('A', libs=['libA.a'], children=[c])
    b = _dep('B', libs=['libB.a'], children=[c])
    root = _dep('R', libs=['libR.a'], children=[a, b])
    libs = dc._get_hierarchical_libs(root)
    assert libs == ['libR.a', 'libA.a', 'libB.a', 'libC.a']
    assert libs.index('libC.a') > libs.index('libA.a')  # shared dep sinks BELOW its users: Unix link order


def test_shared_leaf_is_not_repeated_once_per_path():
    # a dep reachable through many parents: a raw per-path DFS emits one copy per path on the link line
    leaf = _dep('ReCpp', libs=['libReCpp.a'])
    mids = [_dep(f'M{i}', libs=[f'libM{i}.a'], children=[leaf]) for i in range(5)]
    root = _dep('root', libs=['libroot.a'], children=mids)
    libs = dc._get_hierarchical_libs(root)
    assert libs.count('libReCpp.a') == 1
    assert libs[-1] == 'libReCpp.a'   # last, because every M references it


def test_syslibs_follow_the_static_libs():
    c = _dep('C', libs=['libC.a'], syslibs=['pthread'])
    root = _dep('R', libs=['libR.a'], syslibs=['dl'], children=[c])
    assert dc._get_hierarchical_libs(root) == ['libR.a', 'libC.a', 'dl', 'pthread']


def test_non_library_exports_are_still_filtered_out():
    # _get_exported_libs keeps only linkable artifacts on linux-like targets
    root = _dep('R', libs=['libR.a', 'notes.txt', 'libR.so', 'libR.so.1.2.3'])
    assert dc._get_hierarchical_libs(root) == ['libR.a', 'libR.so', 'libR.so.1.2.3']


@pytest.mark.parametrize('libz_first', [True, False], ids=['libz declared first', 'libz declared last'])
def test_a_dep_shared_by_an_exe_and_a_lib_lands_after_both(libz_first):
    # The Unix trap: exe -> {libz, lib1} and lib1 -> libz. First-seen order 'libz lib1' makes ld drop
    # libz members lib1 needs, so the declaration order must not reach the link line.
    libz = _dep('libz', libs=['libz.a'])
    lib1 = _dep('lib1', libs=['lib1.a'], children=[libz])
    order = [libz, lib1] if libz_first else [lib1, libz]
    assert dc._get_hierarchical_libs(_dep('exe', libs=['exe.a'], children=order)) \
        == ['exe.a', 'lib1.a', 'libz.a']


# -- real-toolchain validation ------------------------------------------------
# The unit tests above assert an ORDER. These build and link real static archives to prove GNU ld needs that order.
_CC = shutil.which('cc') or shutil.which('gcc')
_needs_toolchain = pytest.mark.skipif(not (_CC and shutil.which('ar')),
                                      reason='needs a C toolchain (cc/gcc + ar) to build real archives')

# Separate TUs on purpose: ld pulls an archive member only to resolve an undefined symbol. main() returns 0 on the right defs.
_SYMBOL_SPLITS = [('z_shared', 'z_func'),   # main pulls z_shared, lib1 pulls z_func
                  ('z_func', 'z_shared')]   # crossed: swapping WHICH symbol each consumer uses


def _sources(main_calls, lib1_calls):
    return {
        'z_func.c':   'int z_func(void) { return 1; }\n',
        'z_shared.c': 'int z_shared(void) { return 2; }\n',
        'one.c':      f'int {lib1_calls}(void);\nint one_func(void) {{ return {lib1_calls}() + 10; }}\n',
        'main.c':     f'int one_func(void);\nint {main_calls}(void);\n'
                      f'int main(void) {{ return one_func() + {main_calls}() - 13; }}\n',
    }


def _build_archives(d, main_calls, lib1_calls):
    """exe -> {libz, lib1} and lib1 -> libz, as real .a files."""
    def run(*args):
        r = subprocess.run(args, cwd=str(d), capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
    sources = _sources(main_calls, lib1_calls)
    for name, src in sources.items(): (d / name).write_text(src)
    for name in sources: run(_CC, '-c', name)
    run('ar', 'rcs', 'libz.a', 'z_func.o', 'z_shared.o')
    run('ar', 'rcs', 'lib1.a', 'one.o')


def _link(d, libs):
    return subprocess.run([_CC, 'main.o', *libs, '-o', 'app'], cwd=str(d), capture_output=True, text=True)


def _exe_graph():
    libz = _dep('libz', libs=['libz.a'])
    lib1 = _dep('lib1', libs=['lib1.a'], children=[libz])
    return _dep('exe', children=[libz, lib1])  # an executable exports no libs of its own


@_needs_toolchain
@pytest.mark.parametrize('main_calls,lib1_calls', _SYMBOL_SPLITS)
def test_real_link_fails_with_the_naive_first_seen_order(tmp_path, main_calls, lib1_calls):
    _build_archives(tmp_path, main_calls, lib1_calls)
    r = _link(tmp_path, ['libz.a', 'lib1.a'])   # libz emitted where it was FIRST seen
    assert r.returncode != 0
    # ld had already passed libz.a when lib1 introduced its need, so the unresolved symbol is always the one lib1 wanted
    assert lib1_calls in r.stderr


@_needs_toolchain
@pytest.mark.parametrize('main_calls,lib1_calls', _SYMBOL_SPLITS)
def test_real_link_succeeds_with_the_order_mama_emits(tmp_path, main_calls, lib1_calls):
    _build_archives(tmp_path, main_calls, lib1_calls)
    order = dc._get_hierarchical_libs(_exe_graph())   # not hardcoded: whatever mama actually computes
    assert order == ['lib1.a', 'libz.a']
    r = _link(tmp_path, order)
    assert r.returncode == 0, r.stderr               # ONE copy of libz satisfies exe AND lib1
    assert subprocess.run([str(tmp_path / 'app')]).returncode == 0  # ...and resolved to the RIGHT defs
