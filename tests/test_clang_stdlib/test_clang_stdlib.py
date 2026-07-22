"""Pins clang's -stdlib selection: libc++ by default, libstdc++ after use_gcc_stdlib_for_clang()."""
from testutils import make_mock_local_dep
from mama.build_config import BuildConfig
from mama import cmake_configure as cc


def _clang_target(tmp_path, monkeypatch, gcc_stdlib=False):
    cfg = BuildConfig([])
    cfg.msvc = cfg.macos = cfg.ios = cfg.android = cfg.raspi = False
    cfg.mips = cfg.oclea = cfg.xilinx = cfg.imx8mp = cfg.yocto_linux = None
    cfg.linux = True; cfg.clang = True; cfg.gcc = False; cfg.arch = 'x64'
    if gcc_stdlib: cfg.use_gcc_stdlib_for_clang()  # root mamafile opts in, to link GNU-built prebuilts like Qt
    monkeypatch.setattr(cfg, 'get_gcc_linux_march', lambda: 'x86-64')
    monkeypatch.setattr(cc, '_set_compiler_paths', lambda t, o: None)
    target = make_mock_local_dep(tmp_path, src_dir=tmp_path).target
    target.config = cfg
    cc._default_options(target)
    return target.cmake_cxxflags.get('-stdlib', '')


def test_clang_defaults_to_libcxx(tmp_path, monkeypatch):
    assert _clang_target(tmp_path, monkeypatch) == 'libc++'


def test_use_gcc_stdlib_switches_to_libstdcxx(tmp_path, monkeypatch):
    assert _clang_target(tmp_path, monkeypatch, gcc_stdlib=True) == 'libstdc++'
