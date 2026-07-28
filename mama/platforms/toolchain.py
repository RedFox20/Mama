from __future__ import annotations
from dataclasses import dataclass


@dataclass
class Toolchain:
    """What a platform knows about its own compilers, in build-system-neutral terms.

    A build system renders this. `mama/buildsys/cmake/options.py` turns it into `-D` options.
    No field uses CMake vocabulary except `extra_opts`, which is the escape hatch for options
    that only one build system and one platform understand (the Android NDK variables).

    A platform builds this ONCE, after toolchain discovery, and caches it. See `Platform.toolchain()`.
    """
    system_name: str = 'Linux'   ## the OS built FOR: Linux, Android, Darwin or Windows
    system_processor: str = ''   ## the CPU built FOR: aarch64, armv7-a, x86_64 or mipsel
    system_version: str = ''     ## target OS version. '1' marks a bare embedded target
    cc: str = ''                 ## full path to the C compiler
    cxx: str = ''                ## full path to the C++ compiler
    version: str = ''            ## compiler version, eg '13.3.0'
    tool_prefix: str = ''        ## path prefix for ar, ranlib, strip and readelf
    sysroot: str = ''            ## target root holding the system headers and libs
    include_paths: tuple = ()    ## extra include dirs the compiler does not find by itself
    toolchain_file: str = ''     ## a CMake toolchain file the SDK ships. '' if there is none
    find_root_program: str = ''  ## NEVER or ONLY. '' leaves the find-root modes untouched
    install_rpath: bool = False  ## link with the install rpath. Embedded targets need this
    extra_opts: tuple = ()       ## raw build-system options only this platform needs
