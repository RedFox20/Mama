"""How a build is named: the variant suffix that a build dir AND an artifactory archive both carry, and
the build dir name itself.

ONE module with TWO functions, because a build dir name and an archive name that spell the same axis
differently let a build and the package it uploads disagree about which variant they are. A `memory`
build dir used to say 'memory' while its archive said 'msan', and a coverage build uploaded under the
plain name. A dep calls both functions once at init and stores the results: see
BuildDependency.variant_suffix and BuildDependency.build_dir_name.

`config` is duck-typed on purpose. These are naming rules, not configuration, so BuildConfig does not
carry them, and each function reads only the config fields it names."""

from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .build_config import BuildConfig


# asan/tsan/ubsan/lsan runtimes are mutually incompatible (asan and tsan cannot link together, and ubsan
# combos vary), so a build dir and a package archive must name each one distinctly.
_SANITIZER_SHORT_NAMES = {
    'address':   'asan',
    'thread':    'tsan',
    'leak':      'lsan',
    'undefined': 'ubsan',
    'memory':    'msan',
}


def build_variant_suffix(config: BuildConfig, dep_args=()) -> str:
    """Every axis that makes a build unique beyond the platform, the arch and the compiler: coverage, the
    sanitizers, then the dep args. Coarsest axis first, each token with its own '-', and '' for a plain
    build with no args, so an existing name stays byte-identical.

    THE one place a variant is spelled. Both the build dir name and the archive name carry this string,
    so they cannot disagree. The compiler is NOT in here: the build dir names it as a token, and the
    archive name already has a field for its full version.

    Dep args come from the consumer's `add_git(..., args=[...])`, so they are known before the clone, the
    same way the platform and the compiler are. Sorted, lowercased, de-duplicated and stripped of
    punctuation, so the call order, the letter case and a repeated arg never change a name. A '+' becomes
    'p' ('C++20' -> 'cpp20'), and a key=value arg keeps both halves ('NEWMATH=1' -> 'newmath1', which
    stays distinct from 'NEWMATH=2'). `isalnum() and isascii()`, not `isalnum()` alone: isalnum() answers
    True for a letter in any script, and these tokens become a file name on an FTP server and on a
    Windows disk."""
    tokens = ['cov'] if config.coverage else []
    if config.sanitize:
        tokens += [_SANITIZER_SHORT_NAMES.get(s, s) for s in
                   filter(None, (s.strip() for s in config.sanitize.split(',')))]
    if dep_args:
        safe = {''.join(c for c in a.lower().replace('+', 'p') if c.isalnum() and c.isascii()) for a in dep_args}
        tokens += sorted(filter(None, safe))
    return ''.join('-' + t for t in tokens)


def build_dir_name(config: BuildConfig, variant_suffix=None, platform_dir=None) -> str:
    """The build folder name: the platform dir, the compiler, then the variant, coarsest axis first, eg
    'linux', 'windows32', 'linux-clang-cov-asan-lgpl'. A 64-bit arch uses the bare platform name and
    a 32-bit arch adds a suffix, which the platform itself decides.

    '-clang' only on a linux clang build: a shared dir means one compiler clobbers the other and then g++
    links libc++ archives. gcc keeps the bare 'linux' so existing trees do not churn, and elsewhere the
    toolset or the SDK fixes the compiler.

    `variant_suffix` defaults to the config-only variant, for a caller that names no single dep.
    `platform_dir` overrides the config's own platform dir, for the generated mama.cmake that has to name
    every arch's dir."""
    if variant_suffix is None: variant_suffix = build_variant_suffix(config)
    if platform_dir is None: platform_dir = config.platform.build_dir_name() if config.platform else 'build'
    return platform_dir + ('-clang' if (config.linux and config.clang) else '') + variant_suffix
