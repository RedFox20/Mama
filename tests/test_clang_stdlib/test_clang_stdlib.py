"""Pins clang's -stdlib selection: libc++ by default, libstdc++ after use_gcc_stdlib_for_clang()."""
from testutils import make_mock_local_dep, platform_config
from mama.buildsys.cmake import configure as cc
from mama.platforms.linux import Linux


def _clang_target(tmp_path, monkeypatch, gcc_stdlib=False):
    cfg = platform_config(Linux, 'x64', clang=True, gcc=False)
    if gcc_stdlib: cfg.use_gcc_stdlib_for_clang()  # root mamafile opts in, to link GNU-built prebuilts like Qt
    monkeypatch.setattr(cc, '_set_compiler_paths', lambda t, o: None)
    target = make_mock_local_dep(tmp_path, src_dir=tmp_path).target
    target.config = cfg
    cc._default_options(target)
    return target.cmake_cxxflags.get('-stdlib', '')


def test_clang_defaults_to_libcxx(tmp_path, monkeypatch):
    assert _clang_target(tmp_path, monkeypatch) == 'libc++'


def test_use_gcc_stdlib_switches_to_libstdcxx(tmp_path, monkeypatch):
    assert _clang_target(tmp_path, monkeypatch, gcc_stdlib=True) == 'libstdc++'
