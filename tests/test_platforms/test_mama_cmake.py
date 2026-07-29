"""Pins the generated mama.cmake: every platform detectable, every build dir reachable, right order."""
import pytest

from mama.build_config import BuildConfig
from mama.buildsys.cmake.mamacmake import _GUARDS, mama_cmake_text, platform_chain
from mama.platforms.registry import PLATFORMS
from mama.build_names import build_dir_name


def _text():
    return mama_cmake_text(lambda build_dir: f'set(MAMA_BUILD "{build_dir}")')


def test_every_registered_platform_has_a_guard():
    """A platform with no guard is invisible to a consumer's CMakeLists, whatever mama builds."""
    assert set(_GUARDS) == {p.name for p in PLATFORMS}


@pytest.mark.parametrize('platform_class', PLATFORMS, ids=lambda p: p.name)
def test_every_build_dir_is_reachable(platform_class):
    """The anti-drift check. mama writes packages/<target>/<build dir>, and this chain is how the
    consumer finds it again, so a build dir missing here breaks the include with no error."""
    text = _text()
    for arch in platform_class.supported_arches:
        config = BuildConfig([])
        config.set_platform_class(platform_class)
        config.arch = arch
        assert f'set(MAMA_BUILD "{build_dir_name(config)}")' in text


def test_the_specific_guards_come_before_the_generic_ones():
    """android is also UNIX and iOS is also APPLE. Test UNIX first and every android build reads the
    linux build dir."""
    chain = platform_chain(lambda build_dir: '')
    order = [chain.index(f'({_GUARDS[p.name]})') for p in PLATFORMS]
    assert order == sorted(order)
    assert chain.index('(ANDROID OR ANDROID_NDK)') < chain.index('(UNIX)')
    assert chain.index('(APPLE AND IOS_PLATFORM)') < chain.index('(APPLE)\n')


def test_the_x64_arch_is_matched_before_x86():
    """The x86 pattern also matches x86_64, so testing it first sends every x64 build to linux32."""
    chain = platform_chain(lambda build_dir: '')
    assert chain.index('(x86_64)|(X86_64)') < chain.index('"(X86)|(x86)|(i386)|(i686)"')


@pytest.mark.parametrize('define', ['OCLEA', 'XILINX', 'IMX8MP', 'MIPS', 'YOCTO_LINUX'])
def test_a_board_define_reaches_the_consumer_project(define):
    text = _text()
    assert f'add_compile_definitions({define}=1)' in text and f'set({define} TRUE)' in text


@pytest.mark.parametrize('var', ['LINUX', 'MACOS', 'IOS'])
def test_the_historic_platform_variables_still_exist(var):
    assert f'set({var} TRUE)' in _text()


def test_an_unknown_platform_or_arch_fails_loudly():
    """Falling through silently would point MAMA_BUILD at nothing and link no dependency at all."""
    text = _text()
    assert 'message(FATAL_ERROR "mama build: Unsupported Platform!' in text
    assert text.count('Unrecognized') >= 4  # one per multi-arch platform
