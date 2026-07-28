"""Pins that the ROOT mamafile's settings() runs BEFORE the cross-compile toolchain is resolved,
so its set_*_toolchain() wins over the default SDK search paths on both execution paths."""
import pytest
from testutils import make_mock_local_dep
from mama import main as mama_main
from mama.build_config import BuildConfig
from mama.platforms.imx8mp import Imx8mp


class FakeYocto(Imx8mp):
    """A real Yocto board whose SDK probe is recorded instead of hitting the disk."""
    def __init__(self, config):
        super().__init__(config)
        self.probes = []
    def init_toolchain(self, toolchain_dir=None, toolchain_file=None):
        self.probes.append(toolchain_dir)
        self.toolchain_dir = toolchain_dir or '/opt/default-sdk'
        self.compilers = f'{self.toolchain_dir}/bin/'
        self.cc_prefix = f'{self.compilers}aarch64-poky-linux-'


ROOT_MAMAFILE = '''import mama
class project(mama.BuildTarget):
    def settings(self): self.config.set_yocto_toolchain('/opt/custom-sdk')
    def dependencies(self): pass
'''


@pytest.fixture
def yocto_project(tmp_path, monkeypatch):
    """A root project whose settings() picks a custom SDK dir, on a faked imx8mp platform."""
    monkeypatch.setattr('mama.build_config.Imx8mp', FakeYocto)
    monkeypatch.setattr(BuildConfig, 'get_gcc_clang_fullversion', lambda self, cc, dumpfullversion: '13.3')
    (tmp_path / 'mamafile.py').write_text(ROOT_MAMAFILE)
    (tmp_path / 'CMakeLists.txt').write_text('')
    return tmp_path


def test_unified_dispatch_does_not_probe_the_toolchain_before_loading_the_root(yocto_project, monkeypatch):
    probes_at_dispatch, configs = [], []
    def fake_unified(root, scope=None):
        probes_at_dispatch.extend(root.config.yocto_linux.probes)
        configs.append(root.config)
        root.load()  # the scheduler's first job: root LOAD -> settings() -> set_yocto_toolchain()
    monkeypatch.setattr(mama_main, 'execute_unified', fake_unified)
    mama_main.mamabuild(['imx8mp', 'build'], source_dir=str(yocto_project))

    assert probes_at_dispatch == []  # a probe here resolves the DEFAULT sdk before settings() can speak
    config = configs[0]
    assert config.yocto_linux.toolchain_dir == '/opt/custom-sdk'
    assert config.cc_path == '/opt/custom-sdk/bin/aarch64-poky-linux-gcc'


def test_root_load_resolves_the_toolchain_after_settings(tmp_path):
    """Both paths get their toolchain from the root's load(), so the ordering lives there, not in main."""
    src = tmp_path / 'src'; src.mkdir()
    dep = make_mock_local_dep(tmp_path, src_dir=src)
    dep.is_root = True
    calls = []
    dep.target.settings = lambda: calls.append('settings')
    dep.config.init_platform_toolchain.side_effect = lambda: calls.append('toolchain')
    dep.config.get_preferred_compiler_paths.side_effect = lambda: calls.append('compiler')
    dep.load()

    assert calls == ['settings', 'toolchain', 'compiler']


def test_init_platform_toolchain_keeps_the_dir_settings_already_chose(monkeypatch):
    monkeypatch.setattr('mama.build_config.Imx8mp', FakeYocto)
    config = BuildConfig(['imx8mp', 'build'])
    config.set_yocto_toolchain('/opt/custom-sdk')
    config.init_platform_toolchain()

    assert config.yocto_linux.probes == ['/opt/custom-sdk']  # no second, default-path probe
    assert config.yocto_linux.toolchain_dir == '/opt/custom-sdk'
