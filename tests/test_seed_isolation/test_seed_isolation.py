"""Pins that a seed carries its own platform's defaults and that two platforms cannot clobber each other."""
import os
import threading
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from testutils import make_configured_target, set_mock_platform
from mama.buildsys.cmake import configure as cc
from mama.buildsys.cmake import compiler_cache as seedcache
from mama.platforms.imx8mp import Imx8mp



# --- a seed is named for the platform it was detected on ---

def _seed_id_for(tmp_path, **overrides):
    tmp_path.mkdir(parents=True, exist_ok=True)
    t, _ = make_configured_target(tmp_path, **overrides)
    with patch.object(seedcache, 'compute_fingerprint', return_value='deadbeef'):
        return cc._seed_id(t)


def test_the_seed_id_names_the_platform_and_arch(tmp_path):
    t, dep = make_configured_target(tmp_path, arch='arm64')
    dep.config.platform_build_dir_name.return_value = 'android'
    with patch.object(seedcache, 'compute_fingerprint', return_value='deadbeef'):
        assert cc._seed_id(t) == 'android-arm64-deadbeef'


def test_android_and_linux_cannot_share_a_seed_dir_even_on_a_hash_collision(tmp_path):
    """The hash already covers platform+arch, but a host seed reaching a cross build dir is silent and
    catastrophic: cmake sees CMAKE_PLATFORM_INFO_INITIALIZED, skips system determination, never runs the
    toolchain file, and the project compiles with host flags. The NAME keeps them apart regardless."""
    linux = _seed_id_for(tmp_path / 'a', arch='x64')
    (tmp_path / 'b').mkdir(parents=True, exist_ok=True)
    android_t, android_dep = make_configured_target(tmp_path / 'b', arch='arm64')
    android_dep.config.platform_build_dir_name.return_value = 'android'
    with patch.object(seedcache, 'compute_fingerprint', return_value='deadbeef'):
        android = cc._seed_id(android_t)
    assert linux != android and linux.startswith('linux-') and android.startswith('android-')


def test_two_platforms_publish_into_separate_dirs_concurrently(tmp_path):
    """Simultaneous `mama build android` and `mama build linux` must not overwrite each other's seed."""
    root = str(tmp_path / 'seeds')
    built = {}
    def publish(name):
        seed_dir = os.path.join(root, name)
        files_dir = tmp_path / name / 'CMakeFiles'
        files_dir.mkdir(parents=True)
        for lang in ('C', 'CXX'):
            (files_dir / f'CMake{lang}Compiler.cmake').write_text(
                f'set(CMAKE_SYSTEM_NAME "{name}")\nset(CMAKE_{lang}_ABI_COMPILED TRUE)\n')
        (files_dir / 'CMakeSystem.cmake').write_text(f'set(CMAKE_SYSTEM_NAME "{name}")\n')
        built[name] = seedcache.publish(seed_dir, str(files_dir), fingerprint=name)
    threads = [threading.Thread(target=publish, args=(n,)) for n in ('android-arm64-aaaa', 'linux-x64-bbbb')]
    for t in threads: t.start()
    for t in threads: t.join()

    assert all(built.values()), built
    for name in ('android-arm64-aaaa', 'linux-x64-bbbb'):
        system = open(os.path.join(root, name, 'CMakeSystem.cmake')).read()
        assert f'"{name}"' in system  # each seed still holds ITS OWN detected system, not the other's
        assert seedcache.load(os.path.join(root, name))['fingerprint'] == name


# --- the probe detects with the platform's own settings ---

def test_the_seed_probe_configures_with_the_platform_settings(tmp_path):
    """'Inherits actual platform defaults': the throwaway probe project gets the SAME cross opts as a real
    target, so its CMakeSystem.cmake records the cross system rather than the host's."""
    t, dep = make_configured_target(tmp_path, arch='arm64')
    yocto = set_mock_platform(dep.config, Imx8mp)
    yocto.compilers = '/opt/sdk/bin/'
    yocto.cc_prefix = '/opt/sdk/bin/aarch64-poky-linux-'
    yocto.sysroot_path = '/opt/sdk/sysroot'
    yocto.toolchain_file = '/opt/sdk/tc.cmake'
    cmds = []
    with patch.object(cc.SubProcess, 'run', side_effect=lambda cmd, *a, **k: cmds.append(cmd) or 1):
        with cc._probe_toolchain(t) as probe:
            assert probe is None  # run() returned nonzero: we only care about the command it built
    assert '-DCMAKE_SYSTEM_NAME=Linux' in cmds[0]
    assert '-DCMAKE_SYSTEM_PROCESSOR=aarch64' in cmds[0]   # the target's, never the host's
    assert '-DCMAKE_TOOLCHAIN_FILE="/opt/sdk/tc.cmake"' in cmds[0]
    assert '-DCMAKE_C_COMPILER=' not in cmds[0]            # the toolchain file owns that choice


def test_the_registry_lists_every_platform_mama_can_build_for():
    """_platform_opts dispatches through config.platform, and set_platform() builds that from the
    registry - a platform missing from it can never be selected at all."""
    from mama.platforms.registry import PLATFORMS
    from mama.build_config import BuildConfig
    registered = {p.__name__ for p in PLATFORMS}
    dispatched = {name for _, name in BuildConfig._PLATFORM_FLAGS}
    assert dispatched == registered


def test_a_native_build_contributes_no_cross_options(tmp_path):
    t, dep = make_configured_target(tmp_path)   # plain linux host build
    assert cc._platform_opts(t) == []


# --- seed init + validation, mechanically checked for EVERY known platform ---

# (platform build-dir name, target arch) for every platform mama can build for. This is the whole matrix
# the seed cache has to keep apart: any two of these sharing a seed dir means one platform's detected
# toolchain drives another's build.
_ALL_PLATFORMS = [('windows', 'x64'), ('windows32', 'x86'), ('windows-arm64', 'arm64'),
                  ('linux', 'x64'), ('linux32', 'x86'), ('linux-arm64', 'arm64'),
                  ('macos', 'x64'), ('macos-arm64', 'arm64'), ('ios', 'arm64'),
                  ('android', 'arm64'), ('android32', 'arm'), ('raspi', 'arm64'), ('raspi32', 'arm'),
                  ('oclea', 'arm64'), ('xilinx', 'arm64'), ('imx8mp', 'arm64'), ('mips', 'mips')]


def _seed_ids_for_every_platform(tmp_path):
    """One seed id per platform, with the hash forced identical so ONLY the naming keeps them apart."""
    ids = {}
    for i, (platform, arch) in enumerate(_ALL_PLATFORMS):
        sub = tmp_path / f'p{i}'; sub.mkdir(parents=True)
        t, dep = make_configured_target(sub, arch=arch)
        dep.config.platform_build_dir_name.return_value = platform
        with patch.object(seedcache, 'compute_fingerprint', return_value='samehash'):
            ids[(platform, arch)] = cc._seed_id(t)
    return ids


def test_every_platform_gets_its_own_seed_dir(tmp_path):
    ids = _seed_ids_for_every_platform(tmp_path)
    assert len(set(ids.values())) == len(_ALL_PLATFORMS), f'seed dir collision: {ids}'


@pytest.mark.parametrize('platform,arch', _ALL_PLATFORMS)
def test_a_seed_id_names_its_own_platform_and_arch(platform, arch, tmp_path):
    t, dep = make_configured_target(tmp_path, arch=arch)
    dep.config.platform_build_dir_name.return_value = platform
    with patch.object(seedcache, 'compute_fingerprint', return_value='samehash'):
        assert cc._seed_id(t) == f'{platform}-{arch}-samehash'


def test_a_seed_published_for_one_platform_never_validates_for_another(tmp_path):
    """Validation, not just naming: even handed the wrong seed's manifest, is_valid must reject it."""
    ids = list(_seed_ids_for_every_platform(tmp_path).values())
    root = tmp_path / 'seeds'
    for seed_id in ids:
        files = tmp_path / 'det' / seed_id; files.mkdir(parents=True)
        for lang in ('C', 'CXX'):
            (files / f'CMake{lang}Compiler.cmake').write_text(f'set(CMAKE_{lang}_ABI_COMPILED TRUE)\n')
        assert seedcache.publish(str(root / seed_id), str(files), fingerprint=seed_id)

    for seed_id in ids:
        manifest = seedcache.load(str(root / seed_id))
        assert seedcache.is_valid(manifest, seed_id)                       # its own id validates
        for other in ids:
            if other != seed_id:
                assert not seedcache.is_valid(manifest, other), f'{seed_id} accepted as {other}'
