"""Pins the pre-build banner: command verb, target count when known, the TARGET platform, and the toolchain."""
from types import SimpleNamespace
import pytest
from mama._version import __version__
from mama.dependency_chain import print_build_banner
from mama.platforms.linux import Linux
from mama.platforms.windows import Windows
from mama.platforms.android import Android


def _cfg(cc='/usr/bin/gcc', ver='14.3.0', platform_class=Linux, **over):
    c = SimpleNamespace(rebuild=False, update=False, clean=False, build=True,
                        msvc=False, linux=True, clang=False, gcc=True, clang_stdlib='libc++',
                        android=None, arch='x64', verbose=False)
    c.name = lambda: 'linux'
    c.get_preferred_compiler_paths = lambda: (cc, cc, ver)
    c.get_msvc_tools_path = lambda: 'C:/BuildTools/VC/Tools/MSVC/14.44.35207/'
    for k, v in over.items(): setattr(c, k, v)
    c.platform = platform_class(c)
    return c


def _banner(capsys, config, count=None):
    print_build_banner(config, count)
    return capsys.readouterr().out.strip()


@pytest.mark.parametrize('flags,verb', [({}, 'building'), ({'update': True}, 'updating'),
                                        ({'rebuild': True, 'clean': True}, 'rebuilding'),
                                        ({'clean': True, 'build': False}, 'cleaning')])
def test_verb_follows_the_command(capsys, flags, verb):
    assert _banner(capsys, _cfg(**flags)) == f'Mama {__version__} {verb} linux x64 with gcc 14.3'


def test_counts_targets_only_when_known(capsys):
    assert _banner(capsys, _cfg(), 26) == f'Mama {__version__} building 26 target(s) linux x64 with gcc 14.3'
    assert _banner(capsys, _cfg()) == f'Mama {__version__} building linux x64 with gcc 14.3'  # unified: graph still growing


def test_toolchain_names_the_clang_stdlib_on_linux(capsys):
    assert _banner(capsys, _cfg('/usr/bin/clang', clang_stdlib='libstdc++'), 3) \
        == f'Mama {__version__} building 3 target(s) linux x64 with clang 14.3 libstdc++'
    assert _banner(capsys, _cfg(msvc=True, linux=False, platform_class=Windows)) \
        == f'Mama {__version__} building windows x64 with msvc 14.44'
    # off linux the stdlib is not a choice, so the banner omits it
    assert _banner(capsys, _cfg('/usr/bin/clang', linux=False)) \
        == f'Mama {__version__} building linux x64 with clang 14.3'


def test_toolchain_names_the_cross_compiler_not_the_host_flags(capsys):
    """A cross build leaves config.gcc/clang describing the HOST, so the flags read the NDK's clang as 'gcc'."""
    ndk = '/Android/ndk/29.0/bin/aarch64-linux-android29-clang'
    assert _banner(capsys, _cfg(ndk, '21.0.0', linux=False)) == f'Mama {__version__} building linux x64 with clang 21.0'


def _android(**over):
    """A cross-compiling android config: platform naming must survive the NDK path, not the host."""
    config = _cfg('/Android/ndk/29.0.14206865/bin/aarch64-linux-android36-clang', '21.0.0',
                  platform_class=Android, linux=False, arch='arm64', **over)
    config.platform.android_api = 'android-36'
    config.platform.android_ndk_path = '/Android/ndk/29.0.14206865'
    return config


def test_banner_names_the_android_api_arch_and_ndk(capsys):
    # 'clang 21.0' alone cannot tell an android cross build from a host clang - the platform must say so
    assert _banner(capsys, _android()) \
        == f'Mama {__version__} building android-36 arm64 ndk-29.0.14206865 with clang 21.0'


def test_platform_is_dropped_rather_than_failing_the_banner(capsys):
    # a half-built config must never crash the run just to print a banner
    broken = _android()
    broken.platform.android_ndk = lambda: (_ for _ in ()).throw(RuntimeError('no ndk'))
    assert _banner(capsys, broken) == f'Mama {__version__} building with clang 21.0'
