"""Fake on-disk toolchain trees, so every platform's discovery resolves without a real SDK."""
import os
import pytest

from testutils import is_linux, is_windows, touch_file as _touch
from mama.utils.paths import normalized_path
from mama.platforms.generic_yocto import GenericYocto
from mama.platforms.mips import Mips
from mama.platforms.raspi import Raspi, triple_for_arch
from mama.platforms.aarch64 import Aarch64
from mama.platforms.registry import platform_named


# every env var android toolchain discovery reads, in the order it reads them
_ANDROID_ENVS = ('ANDROID_NDK_LATEST_HOME', 'ANDROID_NDK_HOME', 'ANDROID_NDK_ROOT', 'ANDROID_NDK',
                 'ANDROID_HOME', 'ANDROID_SDK_ROOT')


def make_ndk_tree(root, ndk_version='27.3.13750724') -> str:
    """The Android SDK layout: <sdk>/ndk/<ver>/ with ndk-build, the clang bin dir and the toolchain file.
    Every name follows the host, because the NDK ships ndk-build.cmd and a windows-x86_64 bin dir there."""
    ndk = f'{root}/ndk/{ndk_version}'
    ext = '.cmd' if is_windows() else ''
    _touch(f'{ndk}/ndk-build{ext}')
    _touch(f'{ndk}/build/cmake/android.toolchain.cmake')
    prebuilt = 'windows-x86_64' if is_windows() else 'linux-x86_64'
    for arch in ('aarch64', 'armv7a'):
        for suffix in ('clang', 'clang++'):
            _touch(f'{ndk}/toolchains/llvm/prebuilt/{prebuilt}/bin/{arch}-linux-android29-{suffix}{ext}')
    return ndk


def make_yocto_tree(root, sdk_name, sysroot_name, compiler_name, toolchain_rel) -> str:
    """The Yocto SDK layout: sysroots/<host sdk>/ with the cross gcc, plus sysroots/<target>/."""
    _touch(f'{root}/sysroots/{sdk_name}/{compiler_name}')
    os.makedirs(f'{root}/sysroots/{sysroot_name}/usr/include', exist_ok=True)
    _touch(f'{root}/{toolchain_rel}')
    return root


def make_cross_bin_tree(root, triple) -> str:
    """A distro cross package: bin/<triple>-gcc and nothing else, no sysroot of its own."""
    ext = '.exe' if is_windows() else ''
    for suffix in ('gcc', 'g++'):
        _touch(f'{root}/bin/{triple}-{suffix}{ext}')
    return root


@pytest.fixture
def fake_toolchains(tmp_path, monkeypatch):
    """Point every cross platform's discovery at a fake tree under tmp_path, and stub the compiler
    version probe. Returns the roots, so a test can assert against the paths that reach cmake."""
    root = normalized_path(str(tmp_path / 'sdk'))  # every path mama reports back is forward slash only
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
    aarch64 = make_cross_bin_tree(f'{root}/aarch64', 'aarch64-linux-gnu')

    # a CI runner ships its own Android SDK and sets several of these, and ANDROID_NDK_LATEST_HOME
    # is read FIRST, so the fake NDK only wins once every one of them is gone
    for env in _ANDROID_ENVS: monkeypatch.delenv(env, raising=False)
    monkeypatch.setenv('ANDROID_HOME', f'{root}/android-sdk')
    monkeypatch.setenv('ANDROID_NDK_HOME', ndk)
    monkeypatch.setattr(Raspi, '_search_paths', lambda self: [raspi])
    # its own linux_paths end in /usr, so without this it resolves the HOST's real cross package
    monkeypatch.setattr(Aarch64, '_search_paths', lambda self: [aarch64])
    monkeypatch.setattr(GenericYocto, 'append_env_path', lambda self, paths, env: None)
    _patch_yocto_paths(monkeypatch, {'oclea': oclea, 'imx8mp': imx, 'xilinx': xilinx})
    _patch_mips_paths(monkeypatch, mips)
    return dict(ndk=ndk, oclea=oclea, imx8mp=imx, xilinx=xilinx, raspi=raspi, mips=mips,
                aarch64=aarch64)


def _patch_yocto_paths(monkeypatch, roots):
    """Point each board at ONLY its fake root, so a real /opt SDK on this machine cannot win."""
    for name, root in roots.items():
        monkeypatch.setattr(platform_named(name), 'search_paths', (root,))


def _patch_mips_paths(monkeypatch, root):
    original = Mips.init_toolchain
    def with_fake_root(self, toolchain_dir=None, toolchain_file=None, arch=None):
        return original(self, toolchain_dir or root, toolchain_file, arch)
    monkeypatch.setattr(Mips, 'init_toolchain', with_fake_root)


def _needs_linux_host(param) -> bool:
    """True for a platform whose toolchain refuses a non-Linux host. A Yocto SDK ships Linux binaries
    only, and MIPS raises the same way. A new board inherits the answer from its base class."""
    return isinstance(param, type) and issubclass(param, (GenericYocto, Mips))


@pytest.fixture(autouse=True)
def skip_a_linux_host_platform(request):
    """Skip a parametrized case whose platform needs a Linux host. Only a test that resolves a toolchain
    asks for fake_toolchains, and only such a test reaches the discovery that refuses the host."""
    callspec = getattr(request.node, 'callspec', None)
    if is_linux() or not callspec or 'fake_toolchains' not in request.fixturenames: return
    if any(_needs_linux_host(param) for param in callspec.params.values()):
        pytest.skip('needs a Linux host')


@pytest.fixture(autouse=True)
def stub_compiler_version(monkeypatch):
    """Every platform probes its compiler for a version. The fake trees hold empty files."""
    from mama.build_config import BuildConfig
    monkeypatch.setattr(BuildConfig, 'get_gcc_clang_fullversion', lambda self, cc, dumpfullversion: '13.3.0')
