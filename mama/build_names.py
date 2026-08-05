"""How a build is named: the variant suffix that a build dir AND an artifactory archive both carry, and
the build dir name itself. ONE spelling for both, so a build and its uploaded package never disagree."""

from __future__ import annotations
import re
from typing import TYPE_CHECKING

# `config` is duck-typed on purpose. These are naming rules, not configuration, so BuildConfig does not
# carry them, and each function reads only the config fields it names.
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

    THE one place that spells a variant. Both the build dir name and the archive name carry this string,
    so they cannot disagree. The compiler is NOT in here: the build dir names it as a token, and the
    archive name already has a field for its full version.

    Dep args come from the consumer's `add_git(..., args=[...])`, so mama knows them before the clone,
    the same way it knows the platform and the compiler. Sorted, lowercased, de-duplicated and stripped of
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


_UNSAFE_IN_VERSION = re.compile(r'[^A-Za-z0-9._-]+')


def sanitize_version(raw: str) -> str:
    """A version field safe to use as a file name, or '' for an empty pin. Keeps letters, digits, dot,
    dash and underscore, and collapses every other run into one '-', so the tag `release/1.0` names an
    archive `release-1.0`.

    Keeps the case, and keeps a leading 'v'. A lowercase pass merges the tags `v1.0` and `V1.0` into one
    name, and a stripped 'v' merges `v1.0` and `1.0`. A repo may carry all three, and two sources must
    never share one archive name."""
    return _UNSAFE_IN_VERSION.sub('-', raw).strip('-') if raw else ''


CONFIG_TOKENS = frozenset(_SANITIZER_SHORT_NAMES.values()) | {'cov', 'clang'}


def is_build_dir_of(dir_name: str, config_dir_name: str) -> bool:
    """True when `dir_name` is a build dir of the config that names itself `config_dir_name`: that dir, or
    that dir plus dep-arg tokens, because a dep the consumer added with args=[...] gets its own.

    `mama <platform> clean all` cleans ONE config, so a dir that carries another config's token (a
    sanitizer, cov, clang) is not ours even though it starts with the same platform name. A dep arg that
    happens to spell a config token reads as another config's dir and stays: a wrong delete costs more
    than a leftover dir."""
    if dir_name == config_dir_name: return True
    if not dir_name.startswith(config_dir_name + '-'): return False
    return not (set(dir_name[len(config_dir_name) + 1:].split('-')) & CONFIG_TOKENS)


def build_dir_name(config: BuildConfig, variant_suffix=None, platform_dir=None) -> str:
    """The build folder name: the platform dir, the compiler, then the variant, coarsest axis first, eg
    'linux', 'windows32', 'linux-clang-cov-asan-lgpl'. The platform maps each arch to its own dir name.
    Its primary arch uses the bare platform name, and every other arch gets a name of its own.

    '-clang' only on a linux clang build: a shared dir means one compiler clobbers the other and then g++
    links libc++ archives. gcc keeps the bare 'linux' so existing trees do not churn, and elsewhere the
    toolset or the SDK fixes the compiler.

    `variant_suffix` defaults to the config-only variant, for a caller that names no single dep.
    `platform_dir` overrides the config's own platform dir, for the generated mama.cmake that has to name
    every arch's dir."""
    if variant_suffix is None: variant_suffix = build_variant_suffix(config)
    if platform_dir is None: platform_dir = config.platform.build_dir_name() if config.platform else 'build'
    return platform_dir + ('-clang' if (config.linux and config.clang) else '') + variant_suffix
