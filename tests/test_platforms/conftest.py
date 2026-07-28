"""Fake on-disk toolchain trees, so every platform's discovery resolves without a real SDK."""
import os
import pytest

from mama.platforms.generic_yocto import GenericYocto
from mama.platforms.mips import Mips
from mama.platforms.raspi import Raspi, triple_for_arch
from mama.platforms.registry import platform_named


def _touch(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f: f.write('')


def make_ndk_tree(root, ndk_version='27.3.13750724') -> str:
    """The Android SDK layout: <sdk>/ndk/<ver>/ with ndk-build, the clang bin dir and the toolchain file."""
    ndk = f'{root}/ndk/{ndk_version}'
    _touch(f'{ndk}/ndk-build')
    _touch(f'{ndk}/build/cmake/android.toolchain.cmake')
    for arch in ('aarch64', 'armv7a'):
        for suffix in ('clang', 'clang++'):
            _touch(f'{ndk}/toolchains/llvm/prebuilt/linux-x86_64/bin/{arch}-linux-android29-{suffix}')
    return ndk


def make_yocto_tree(root, sdk_name, sysroot_name, compiler_name, toolchain_rel) -> str:
    """The Yocto SDK layout: sysroots/<host sdk>/ with the cross gcc, plus sysroots/<target>/."""
    _touch(f'{root}/sysroots/{sdk_name}/{compiler_name}')
    os.makedirs(f'{root}/sysroots/{sysroot_name}/usr/include', exist_ok=True)
    _touch(f'{root}/{toolchain_rel}')
    return root


def make_cross_bin_tree(root, triple) -> str:
    """A distro cross package: bin/<triple>-gcc and nothing else, no sysroot of its own."""
    for suffix in ('gcc', 'g++'):
        _touch(f'{root}/bin/{triple}-{suffix}')
    return root


@pytest.fixture
def fake_toolchains(tmp_path, monkeypatch):
    """Point every cross platform's discovery at a fake tree under tmp_path, and stub the compiler
    version probe. Returns the roots, so a test can assert against the paths that reach cmake."""
    root = str(tmp_path / 'sdk')
    ndk = make_ndk_tree(f'{root}/android-sdk')
    oclea = make_yocto_tree(f'{root}/oclea/1.0', 'x86_64-ocleasdk-linux', 'cortexa53-oclea-linux',
                            'usr/bin/aarch64-oclea-linux/aarch64-oclea-linux-gcc',
                            'aarch64_oclea_toolchain.cmake')
    imx = make_yocto_tree(f'{root}/imx/5.0.4', 'x86_64-pokysdk-linux', 'cortexa53-crypto-poky-linux',
                          'usr/bin/aarch64-poky-linux/aarch64-poky-linux-gcc',
                          'sysroots/x86_64-pokysdk-linux/usr/share/cmake/'
                          'cortexa53-crypto-poky-linux-toolchain.cmake')
    xilinx = make_yocto_tree(f'{root}/petalinux/1.0.0', 'x86_64-petalinux-linux',
                             'cortexa72-cortexa53-xilinx-linux',
                             'usr/bin/aarch64-xilinx-linux/aarch64-xilinx-linux-gcc',
                             'aarch64_xilinx_toolchain.cmake')
    raspi = f'{root}/raspi'
    for arch in Raspi.supported_arches:
        make_cross_bin_tree(raspi, triple_for_arch(arch))
    mips = make_cross_bin_tree(f'{root}/mips', 'mipsel-linux-gnu')

    monkeypatch.setenv('ANDROID_HOME', f'{root}/android-sdk')
    monkeypatch.setenv('ANDROID_NDK_HOME', ndk)
    monkeypatch.setattr(Raspi, '_search_paths', lambda self: [raspi])
    monkeypatch.setattr(GenericYocto, 'append_env_path', lambda self, paths, env: None)
    _patch_yocto_paths(monkeypatch, {'oclea': oclea, 'imx8mp': imx, 'xilinx': xilinx})
    _patch_mips_paths(monkeypatch, mips)
    return dict(ndk=ndk, oclea=oclea, imx8mp=imx, xilinx=xilinx, raspi=raspi, mips=mips)


def _patch_yocto_paths(monkeypatch, roots):
    """Point each board at ONLY its fake root, so a real /opt SDK on this machine cannot win."""
    for name, root in roots.items():
        monkeypatch.setattr(platform_named(name), 'search_paths', (root,))


def _patch_mips_paths(monkeypatch, root):
    original = Mips.init_toolchain
    def with_fake_root(self, toolchain_dir=None, toolchain_file=None, arch=None):
        return original(self, toolchain_dir or root, toolchain_file, arch)
    monkeypatch.setattr(Mips, 'init_toolchain', with_fake_root)


@pytest.fixture(autouse=True)
def stub_compiler_version(monkeypatch):
    """Every platform probes its compiler for a version. The fake trees hold empty files."""
    from mama.build_config import BuildConfig
    monkeypatch.setattr(BuildConfig, 'get_gcc_clang_fullversion', lambda self, cc, dumpfullversion: '13.3.0')
