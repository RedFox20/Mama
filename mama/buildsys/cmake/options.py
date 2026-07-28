from __future__ import annotations
from typing import TYPE_CHECKING
import os

from mama.platforms.toolchain import Toolchain

if TYPE_CHECKING:
    from mama.build_target import BuildTarget
    from mama.build_config import BuildConfig
    from mama.platforms.platform import Platform


def use_toolchain_file(config:BuildConfig, toolchain:str) -> str:
    """Record the toolchain file a platform picked and return its cmake option. Every platform that has
    one routes through here, so `config.cmake_toolchain_file` answers "is a toolchain file in play" with
    one bool read - nothing has to scan the option list.

    It also decides whether mama may name the compiler. A toolchain file REWRITES that choice: the
    Android NDK's takes our `bin/aarch64-linux-android29-clang` and puts `bin/clang` in the cache,
    driving the target with `--target=` instead. Same compiler, different string - and the string is all
    cmake compares. On a build dir that already holds a cache (a warm dir, or one the compiler seed
    pre-populated) our -DCMAKE_C_COMPILER then reads as a CHANGED variable, so cmake deletes the cache
    and re-runs. That second pass loses the seeded platform info and re-detects, which is how a cross
    build ends up compiling with host flags."""
    config.cmake_toolchain_file = toolchain
    return f'CMAKE_TOOLCHAIN_FILE="{toolchain}"'


def _toolchain_file(target:BuildTarget, platform:Platform, tc:Toolchain) -> str:
    """The toolchain file this build uses. A mamafile can override the platform's own through the
    target attribute the platform names, eg `cmake_ndk_toolchain`."""
    attr = platform.toolchain_override_attr
    override = getattr(target, attr, '') if attr else ''
    if override:
        path = override if os.path.isabs(override) else target.source_dir(override)
        # a platform with no default of its own still passes a bad path, so cmake reports it
        if os.path.exists(path) or not tc.toolchain_file: return path
    return tc.toolchain_file


def platform_opts(target:BuildTarget) -> list:
    """Render the active platform's Toolchain into cmake options.

    This is the ONLY place a platform fact becomes a cmake option. A platform describes what it needs
    (see mama/platforms/toolchain.py) and never formats a `-D` flag, so a second build system reads the
    same description and writes its own.

    Config-level only, no project flags, so the seed probe and the seed fingerprint can both use it and
    stay target-independent."""
    config:BuildConfig = target.config
    platform = config.platform
    tc = platform.toolchain()

    opts = []
    if platform.platform_define: opts.append(f'{platform.platform_define}=TRUE')
    if tc.host_toolset: opts.append(f'CMAKE_GENERATOR_TOOLSET=host={tc.host_toolset}')
    if platform.is_cross:
        # EVERY cross platform emits both. Leaving the processor to the toolchain file is what broke
        # android: the compiler seed writes CMAKE_PLATFORM_INFO_INITIALIZED, cmake then skips system
        # determination, the toolchain file never runs, and the processor falls back to the host's.
        opts.append(f'CMAKE_SYSTEM_NAME={tc.system_name}')
        if tc.system_processor: opts.append(f'CMAKE_SYSTEM_PROCESSOR={tc.system_processor}')
    opts += list(tc.extra_opts)

    toolchain = _toolchain_file(target, platform, tc)
    if toolchain:
        opts.append(use_toolchain_file(config, toolchain))
        config.announce_once('toolchain', f'Toolchain: {toolchain}')
        if tc.toolchain_file_is_complete:
            return opts  # the SDK's own file already sets the sysroot, the tools and the find modes

    if tc.system_version: opts.append(f'CMAKE_SYSTEM_VERSION={tc.system_version}')
    if tc.sysroot: opts.append(f'CMAKE_SYSROOT={tc.sysroot}')
    for var, tool in (('AR', 'ar'), ('READELF', 'readelf'), ('STRIP', 'strip'), ('RANLIB', 'ranlib')):
        if tc.tool_prefix: opts.append(f'CMAKE_{var}={tc.tool_prefix}{tool}')
    if tc.find_root_program:
        opts += [f'CMAKE_FIND_ROOT_PATH_MODE_PROGRAM={tc.find_root_program}',
                 # search for libraries and headers in the target directories only
                 'CMAKE_FIND_ROOT_PATH_MODE_LIBRARY=ONLY',
                 'CMAKE_FIND_ROOT_PATH_MODE_INCLUDE=ONLY']
    if tc.install_rpath: opts.append('CMAKE_BUILD_WITH_INSTALL_RPATH=ON')
    return opts
