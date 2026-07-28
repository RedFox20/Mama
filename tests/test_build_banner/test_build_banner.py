"""Pins the pre-build banner: command verb, target count when known, and the toolchain it builds with."""
from types import SimpleNamespace
import pytest
from mama._version import __version__
from mama.dependency_chain import print_build_banner


def _cfg(cc='/usr/bin/gcc', ver='14.3.0', **over):
    c = SimpleNamespace(rebuild=False, update=False, clean=False, build=True,
                        msvc=False, linux=True, clang=False, gcc=True, clang_stdlib='libc++')
    c.get_preferred_compiler_paths = lambda: (cc, cc, ver)
    c.get_msvc_tools_path = lambda: 'C:/BuildTools/VC/Tools/MSVC/14.44.35207/'
    for k, v in over.items(): setattr(c, k, v)
    return c


def _banner(capsys, config, count=None):
    print_build_banner(config, count)
    return capsys.readouterr().out.strip()


@pytest.mark.parametrize('flags,verb', [({}, 'building'), ({'update': True}, 'updating'),
                                        ({'rebuild': True, 'clean': True}, 'rebuilding'),
                                        ({'clean': True, 'build': False}, 'cleaning')])
def test_verb_follows_the_command(capsys, flags, verb):
    assert _banner(capsys, _cfg(**flags)) == f'Mama {__version__} {verb} with gcc 14.3'


def test_counts_targets_only_when_known(capsys):
    assert _banner(capsys, _cfg(), 26) == f'Mama {__version__} building 26 target(s) with gcc 14.3'
    assert _banner(capsys, _cfg()) == f'Mama {__version__} building with gcc 14.3'  # unified: graph still growing


def test_toolchain_names_the_clang_stdlib_on_linux(capsys):
    assert _banner(capsys, _cfg('/usr/bin/clang', clang_stdlib='libstdc++'), 3) \
        == f'Mama {__version__} building 3 target(s) with clang 14.3 libstdc++'
    assert _banner(capsys, _cfg(msvc=True, linux=False)) == f'Mama {__version__} building with msvc 14.44'
    # off linux the stdlib isn't a choice, so it isn't reported
    assert _banner(capsys, _cfg('/usr/bin/clang', linux=False)) == f'Mama {__version__} building with clang 14.3'


def test_toolchain_names_the_cross_compiler_not_the_host_flags(capsys):
    """A cross build leaves config.gcc/clang describing the HOST, so the flags read the NDK's clang as 'gcc'."""
    ndk = '/Android/ndk/29.0/bin/aarch64-linux-android29-clang'
    assert _banner(capsys, _cfg(ndk, '21.0.0', linux=False)) == f'Mama {__version__} building with clang 21.0'
