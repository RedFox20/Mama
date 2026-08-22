from __future__ import annotations

from mama.platforms.registry import PLATFORMS


# The CMake condition per platform, tested in registry order: android is also UNIX and iOS is also
# APPLE, so the specific guard comes first. A consumer cannot detect a platform missing from here.
_GUARDS = {
    'android': 'ANDROID OR ANDROID_NDK',
    'windows': 'WIN32',
    'ios':     'APPLE AND IOS_PLATFORM',
    'macos':   'APPLE',
    'raspi':   'RASPI',
    'oclea':   'OCLEA',
    'xilinx':  'XILINX',
    'imx8mp':  'IMX8MP',
    'mips':    'MIPS',
    'linux':   'UNIX',
}

# Variables a consumer's CMakeLists has always been able to test, beyond the platform's own define.
_EXTRA_VARS = {'ios': ('IOS',), 'macos': ('MACOS',), 'linux': ('LINUX',)}

# The MAMA_CMAKE_ARCH pattern and the variable each arch sets, tested in THIS order: x64 before x86,
# which also matches x86_64, and mips64el before mips. Each pattern is a union of what
# CMAKE_GENERATOR_PLATFORM, ANDROID_ARCH and CMAKE_SYSTEM_PROCESSOR report for that arch.
_ARCH_MATCH = (
    ('x64',      'MAMA_ARCH_X64',   '(amd64)|(AMD64)|(IA64)|(x64)|(X64)|(x86_64)|(X86_64)'),
    ('x86',      'MAMA_ARCH_X86',   '(X86)|(x86)|(i386)|(i686)'),
    ('arm64',    'MAMA_ARCH_ARM64', '(aarch64)|(AARCH64)|(arm64)|(ARM64)'),
    ('arm',      'MAMA_ARCH_ARM32', '(armv7)|(ARMV7)|(arm)|(ARM)'),
    ('mips64el', 'MAMA_ARCH_MIPS',  '(mips64el)|(MIPS64EL)'),
    ('mips64',   'MAMA_ARCH_MIPS',  '(mips64)|(MIPS64)'),
    ('mipsel',   'MAMA_ARCH_MIPS',  '(mipsel)|(MIPSEL)'),
    ('mips',     'MAMA_ARCH_MIPS',  '(mips)|(MIPS)'),
)


def _platform_header(platform) -> str:
    """The lines every arch of this platform shares: the variables a consumer can test."""
    lines = [f'set({var} TRUE)' for var in _EXTRA_VARS.get(platform.name, ())]
    for define, value in platform.compile_defines.items():
        lines += [f'set({define} TRUE)', f'add_compile_definitions({define}={value})']
    return ''.join(f'    {line}\n' for line in lines)


def _arch_branches(platform, build_dir_defines) -> str:
    """The per-arch body: set the arch variable and point MAMA_BUILD at that arch's build dir. An
    unmatched arch is a FATAL_ERROR: a guessed build dir silently links the wrong architecture's libraries."""
    arches = [a for a in _ARCH_MATCH if a[0] in platform.supported_arches]
    if len(arches) == 1:
        arch, var, _ = arches[0]
        return f'    set({var} TRUE)\n    {build_dir_defines(platform.build_dirs.get(arch, platform.name))}\n'

    body = ''
    for i, (arch, var, pattern) in enumerate(arches):
        test = 'if' if i == 0 else 'elseif'
        body += f'    {test}(MAMA_CMAKE_ARCH MATCHES "{pattern}")\n'
        body += f'        set({var} TRUE)\n'
        body += f'        {build_dir_defines(platform.build_dirs.get(arch, platform.name))}\n'
    body += '    else()\n'
    body += f'        message(FATAL_ERROR "MAMA: Unrecognized {platform.name} architecture' \
            ' \'${MAMA_CMAKE_ARCH}\'")\n'
    body += '    endif()\n'
    return body


def platform_chain(build_dir_defines) -> str:
    """The whole if/elseif chain over every platform, generated from the registry, so the chain and
    BuildConfig read the same build_dirs table and cannot drift apart.
    build_dir_defines: callable that returns the MAMA_BUILD lines for one build dir
    """
    chain = ''
    for i, platform in enumerate(PLATFORMS):
        test = 'if' if i == 0 else 'elseif'
        chain += f'{test}({_GUARDS[platform.name]})\n'
        chain += _platform_header(platform)
        chain += _arch_branches(platform, build_dir_defines)
    chain += 'else()\n'
    chain += '    message(FATAL_ERROR "mama build: Unsupported Platform! \'${MAMA_CMAKE_ARCH}\'")\n'
    chain += 'endif()\n'
    return chain


# A plain string, not part of the f-string below: every `${}` here would need a doubled brace.
_MODULES_HELPER = '''
# The lever: OFF keeps the exported headers of every package, whatever the toolchain can do.
option(MAMA_ENABLE_MODULES "Compile the C++20 modules that mama packages export" ON)

# The least compiler version that builds an exported module. Raise one for a package whose modules
# need a newer compiler than this, and the consumer keeps its exported headers instead.
set(MAMA_MODULES_MIN_GNU   14   CACHE STRING "Least GCC version that builds exported C++20 modules")
set(MAMA_MODULES_MIN_CLANG 18   CACHE STRING "Least Clang version that builds exported C++20 modules")
set(MAMA_MODULES_MIN_MSVC  1934 CACHE STRING "Least MSVC version that builds exported C++20 modules")

# An empty floor must refuse, never pass. Unquoted it breaks the `if`, and quoted it compares TRUE.
foreach(id GNU CLANG MSVC)
    if(NOT MAMA_MODULES_MIN_${id})
        set(MAMA_MODULES_MIN_${id} 999999)
    endif()
endforeach()

# C++20 modules need cmake 3.28, the Ninja or Visual Studio generator, and a compiler that reports
# its import graph. A toolchain that misses one keeps the headers, so a build never fails on this.
set(MAMA_MODULES_AVAILABLE FALSE)
set(MAMA_MODULES_GENERATOR FALSE)
if(CMAKE_GENERATOR MATCHES "^Visual Studio ([0-9]+)")
    # cmake scans a module graph for Visual Studio 17 2022 and newer, never for an older one
    if(CMAKE_MATCH_1 GREATER_EQUAL 17)
        set(MAMA_MODULES_GENERATOR TRUE)
    endif()
elseif(CMAKE_GENERATOR MATCHES "Ninja")
    # a Ninja generator writes a dyndep file, and only ninja 1.11 and newer read one. The probe runs
    # on every configure, because a cached version outlives the executable that answered it.
    execute_process(COMMAND "${CMAKE_MAKE_PROGRAM}" --version ERROR_QUIET
                    OUTPUT_VARIABLE MAMA_NINJA_VERSION OUTPUT_STRIP_TRAILING_WHITESPACE)
    if(MAMA_NINJA_VERSION AND NOT MAMA_NINJA_VERSION VERSION_LESS 1.11)
        set(MAMA_MODULES_GENERATOR TRUE)
    endif()
endif()
if(MAMA_ENABLE_MODULES AND CMAKE_VERSION VERSION_GREATER_EQUAL 3.28 AND MAMA_MODULES_GENERATOR)
    if(CMAKE_CXX_COMPILER_ID MATCHES "Clang")
        # cmake reads a clang import graph with clang-scan-deps, which a split install may not ship.
        # The Visual Studio generator scans a module graph with the MSVC toolset alone, never clang-cl.
        if(NOT CMAKE_GENERATOR MATCHES "^Visual Studio"
           AND CMAKE_CXX_COMPILER_CLANG_SCAN_DEPS AND EXISTS "${CMAKE_CXX_COMPILER_CLANG_SCAN_DEPS}"
           AND CMAKE_CXX_COMPILER_VERSION VERSION_GREATER_EQUAL ${MAMA_MODULES_MIN_CLANG})
            set(MAMA_MODULES_AVAILABLE TRUE)
        endif()
    elseif(CMAKE_CXX_COMPILER_ID STREQUAL "GNU")
        if(CMAKE_CXX_COMPILER_VERSION VERSION_GREATER_EQUAL ${MAMA_MODULES_MIN_GNU})
            set(MAMA_MODULES_AVAILABLE TRUE)
        endif()
    elseif(MSVC AND MSVC_VERSION GREATER_EQUAL ${MAMA_MODULES_MIN_MSVC})
        set(MAMA_MODULES_AVAILABLE TRUE)
    endif()
endif()

# Adds the C++20 modules of every mama package to `target`, once, after the target exists.
# scope is PUBLIC by default. A library that installs itself through install(EXPORT) needs PRIVATE.
function(mama_target_modules target)
    set(scope PUBLIC)
    if(ARGC GREATER 1)
        set(scope "${ARGV1}")
    endif()
    if(NOT MAMA_MODULES_AVAILABLE OR NOT MAMA_MODULES)
        message(STATUS "MAMA: C++20 modules off, using the exported headers")
        return()
    endif()
    # a module needs C++20, and the consumer mamafile does not have to force a standard of its own
    target_compile_features(${target} ${scope} cxx_std_20)
    target_sources(${target} ${scope} FILE_SET mama_modules TYPE CXX_MODULES
                   BASE_DIRS ${MAMA_MODULES_BASE_DIRS} FILES ${MAMA_MODULES})
    target_compile_definitions(${target} ${scope} MAMA_HAS_MODULES=1)
endfunction()
'''


def mama_cmake_text(build_dir_defines) -> str:
    """The `mysource/mama.cmake` proxy a consumer's CMakeLists includes. It detects the platform and
    the arch the way cmake sees them, then includes that build dir's mama-dependencies.cmake."""
    return f'''# This file is auto-generated by mama build. Do not modify by hand!
if(CMAKE_CXX_COMPILER_ID MATCHES "Clang")
    set(CLANG TRUE)
elseif(CMAKE_CXX_COMPILER_ID MATCHES "GNU")
    set(GCC TRUE)
endif()

if(CMAKE_GENERATOR_PLATFORM)
    set(MAMA_CMAKE_ARCH ${{CMAKE_GENERATOR_PLATFORM}})
elseif(ANDROID OR ANDROID_NDK)
    set(MAMA_CMAKE_ARCH ${{ANDROID_ARCH}})
elseif(CMAKE_SYSTEM_PROCESSOR)
    set(MAMA_CMAKE_ARCH ${{CMAKE_SYSTEM_PROCESSOR}})
else()
    message(FATAL_ERROR "MAMA: Missing CMake target architecture!")
endif()

# Initializes the INCLUDE and LIBS, they will overwritten in mama-dependencies.cmake
set(MAMA_INCLUDE "")
set(MAMA_LIBS "")

# Set MAMA_INCLUDES and MAMA_LIBS for each platform
{platform_chain(build_dir_defines)}
# The release CRT on MSVC, for a project that holds policy CMP0091 at OLD. Mama passes one runtime
# library for the whole tree, so this rewrite never has to ask which one.
if(MSVC)
    add_definitions(-D_ITERATOR_DEBUG_LEVEL=0)
    foreach(MODE "_DEBUG" "_MINSIZEREL" "_RELEASE" "_RELWITHDEBINFO")
        string(REPLACE "/MDd" "/MD" TMP "${{CMAKE_C_FLAGS${{MODE}}}}")
        set(CMAKE_C_FLAGS${{MODE}} "${{TMP}}" CACHE STRING "" FORCE)
        string(REPLACE "/MDd" "/MD" TMP "${{CMAKE_CXX_FLAGS${{MODE}}}}")
        set(CMAKE_CXX_FLAGS${{MODE}} "${{TMP}}" CACHE STRING "" FORCE)
    endforeach(MODE)
endif()
{_MODULES_HELPER}'''
