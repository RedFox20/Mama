"""Pins that a forced C++ standard reaches cmake as CMAKE_CXX_STANDARD, and only then."""
import pytest
from testutils import make_mock_local_dep, platform_config

from mama.buildsys.cmake import configure as cc
from mama.platforms.linux import Linux
from mama.platforms.windows import Windows


def _target(tmp_path, monkeypatch, enable=None, cmake='3.28.3', arch='x64', **cfg):
    monkeypatch.setattr(cc, '_set_compiler_paths', lambda t, o: None)
    target = make_mock_local_dep(tmp_path, src_dir=tmp_path).target
    target.config = platform_config(cfg.pop('platform', Linux), arch, **cfg)
    target.config._cmake_ver_num = cmake
    if enable: getattr(target, f'enable_cxx{enable}')()
    return target


def _opts(tmp_path, monkeypatch, **kwargs) -> dict:
    """The `KEY: value` cmake options a configure would pass for this target."""
    target = kwargs.pop('target', None) or _target(tmp_path, monkeypatch, **kwargs)
    opts = target.cmake_opts + cc._default_options(target)
    return dict(o.split('=', 1) for o in opts if '=' in o)


# --- the standard reaches cmake ----------------------------------------------

@pytest.mark.parametrize('enable,expected', [('11', '11'), ('14', '14'), ('17', '17'),
                                             ('20', '20'), ('23', '23'), ('26', '23')])
def test_each_forced_standard_sets_the_cmake_standard(enable, expected, tmp_path, monkeypatch):
    # 26 maps to 23 on gcc and clang, because enable_cxx26 asks for c++2b, which IS C++23
    assert _opts(tmp_path, monkeypatch, enable=enable)['CMAKE_CXX_STANDARD'] == expected


def test_the_standard_is_required_and_the_extensions_are_off(tmp_path, monkeypatch):
    # OFF keeps the flag cmake adds equal to the -std the mamafile already asked for
    opts = _opts(tmp_path, monkeypatch, enable='20')
    assert opts['CMAKE_CXX_STANDARD_REQUIRED'] == 'ON' and opts['CMAKE_CXX_EXTENSIONS'] == 'OFF'


def test_the_raw_std_flag_still_reaches_the_compiler(tmp_path, monkeypatch):
    target = _target(tmp_path, monkeypatch, enable='20')
    _opts(tmp_path, monkeypatch, target=target)
    assert target.cmake_cxxflags['-std'] == 'c++20'


def test_a_yocto_platform_keeps_its_own_c2a_spelling(tmp_path, monkeypatch):
    # these SDKs ship a gcc that predates the final name, and the number is still 20
    from mama.platforms.imx8mp import Imx8mp
    target = _target(tmp_path, monkeypatch, enable='20', platform=Imx8mp, arch='arm64')
    assert target.cmake_cxxflags['-std'] == 'c++2a'
    assert target.cxx_standard() == '20'


# --- and only when the mamafile forced one -----------------------------------

def test_a_mamafile_that_forces_nothing_leaves_the_cmake_default(tmp_path, monkeypatch):
    assert 'CMAKE_CXX_STANDARD' not in _opts(tmp_path, monkeypatch)


def test_a_target_with_no_cxx_build_sets_nothing(tmp_path, monkeypatch):
    target = _target(tmp_path, monkeypatch, enable='20')
    target.disable_cxx_compiler()
    assert 'CMAKE_CXX_STANDARD' not in _opts(tmp_path, monkeypatch, target=target)


def test_a_cmake_too_old_for_the_standard_sets_nothing(tmp_path, monkeypatch):
    # cmake learned C++23 in 3.20, and an older one fails the configure on a number it does not know
    assert 'CMAKE_CXX_STANDARD' not in _opts(tmp_path, monkeypatch, enable='23', cmake='3.16.3')


def test_an_unreadable_cmake_version_sets_nothing(tmp_path, monkeypatch):
    assert 'CMAKE_CXX_STANDARD' not in _opts(tmp_path, monkeypatch, enable='20', cmake='unknown')


def test_the_mamafile_keeps_the_standard_it_named_itself(tmp_path, monkeypatch):
    # add_cmake_options comes first on the command line, so a default of ours would override it
    target = _target(tmp_path, monkeypatch, enable='20')
    target.add_cmake_options('CMAKE_CXX_STANDARD=17')
    opts = _opts(tmp_path, monkeypatch, target=target)
    assert opts['CMAKE_CXX_STANDARD'] == '17'
    assert opts['CMAKE_CXX_STANDARD_REQUIRED'] == 'ON'  # the ones it did not name still apply


# --- the msvc spelling -------------------------------------------------------

def test_msvc_forces_the_standard_without_doubling_the_std_prefix(tmp_path, monkeypatch):
    target = _target(tmp_path, monkeypatch, enable='23', platform=Windows, msvc=True, gcc=False)
    assert target.cmake_cxxflags['/std'] == 'c++23preview'
    assert target.cxx_standard() == '23'
