"""How a build is named: the variant suffix that a build dir AND an artifactory archive both carry, and
the build dir name itself. ONE spelling for both, so a build and its uploaded package never disagree."""

from __future__ import annotations
import re
from typing import TYPE_CHECKING
from .platforms.platform import ARCHES, host_arch
from .platforms.registry import platform_named

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


# gcc spells the 64-bit x86 levels 'x86-64' and 'x86-64-v2' up to 'v4'. mama calls that arch 'x64', and
# the bare baseline is psABI level 1, so 'x64v1' keeps it apart from plain 'x64', which means native.
_MARCH_X64 = 'x86-64'


def _safe_token(raw: str) -> str:
    """`raw` reduced to ASCII alphanumerics, with '+' spelled 'p'. '' when nothing survives. A token
    becomes a file name on an FTP server, so isalnum() alone is not enough: it accepts any script."""
    return ''.join(c for c in raw.lower().replace('+', 'p') if c.isalnum() and c.isascii())


def arch_marker(config: BuildConfig) -> str:
    """The arch field of an artifactory archive name: the target arch, or the -march pin that replaces it.
    The build dir does NOT carry it, see build_dir_name.

    A -march value names the architecture itself, so the pin takes the field instead of repeating the
    axis. 'x64' plus 'x86-64-v3' reads 'x64v3', and 'arm64' plus 'armv8.2-a' reads 'armv82a'. An
    unpinned build keeps the bare arch, byte for byte, and no pin may ever spell that same marker.

    EVERY marker opens with an arch name. A pin that names a CPU instead of an architecture, such as
    'haswell', gets the arch in front of it, because the field it fills has to name the arch."""
    march = config.target_march.get(config.arch)
    if not march: return config.arch
    if march.startswith(_MARCH_X64): march = 'x64' + (march[len(_MARCH_X64):] or '-v1')
    token = _safe_token(march)
    if not token: return config.arch
    return token if token.startswith(ARCHES) else config.arch + token


def build_variant_suffix(config: BuildConfig, dep_args=()) -> str:
    """Every axis that makes a build unique beyond the platform, the arch and the compiler: coverage, the
    sanitizers, then the dep args. Coarsest axis first, each token with its own '-', and '' for a plain
    build with no args, so an existing name stays byte-identical.

    THE one place that spells a variant. Both the build dir name and the archive name carry this string,
    so they cannot disagree. The compiler is NOT in here: the build dir names it as a token, and the
    archive name already has a field for its full version. Neither is the -march pin, which belongs to
    the arch and merges into it, see arch_marker.

    Dep args come from the consumer's `add_git(..., args=[...])`, so mama knows them before the clone,
    the same way it knows the platform and the compiler. Sorted, lowercased, de-duplicated and stripped of
    punctuation, so the call order, the letter case and a repeated arg never change a name. A '+' becomes
    'p' ('C++20' -> 'cpp20'), and a key=value arg keeps both halves ('NEWMATH=1' -> 'newmath1', which
    stays distinct from 'NEWMATH=2')."""
    tokens = ['cov'] if config.coverage else []
    if config.sanitize:
        tokens += [_SANITIZER_SHORT_NAMES.get(s, s) for s in
                   filter(None, (s.strip() for s in config.sanitize.split(',')))]
    if dep_args:
        tokens += sorted(filter(None, {_safe_token(a) for a in dep_args}))
    return ''.join('-' + t for t in tokens)


def build_dir_build_type(dep) -> str:
    """`release`, `debug`, or '' when the build dir names no type. Debug and release share one build
    dir, so only the cmake cache says which type the artifacts in it came from. A multi-config
    generator picks the type per build and keeps both, so its dir answers ''."""
    from .buildsys.cmake.configure import cached_build_type  # local import: avoid a cycle
    recorded = cached_build_type(dep.build_dir, single_config_only=True)
    return ('debug' if recorded == 'Debug' else 'release') if recorded else ''


def build_type_of(target) -> str:
    """`release` or `debug`: what the artifacts of this target ARE. The build dir answers first, because
    a run that built nothing leaves the artifacts of the last one. The run answers when the dir cannot."""
    return build_dir_build_type(target.dep) or ('release' if target.config.release else 'debug')


def object_attributes(target) -> str:
    """Every axis that decides whether a consumer can link these objects: the build type, the platform,
    the arch, then the variant tokens. The `O` record of papa.txt carries them, space separated.

    An -march pin follows the arch as `march=x86-64-v3`, its real value. A record is text, not a file
    name, and a reader compares this one against a CPU."""
    config = target.config
    march = config.target_march.get(config.arch)
    attrs = f'{build_type_of(target)} {config.name()} {config.arch}'
    if march: attrs += f' march={march}'
    return attrs + target.dep.variant_suffix.replace('-', ' ')


_UNSAFE_IN_VERSION = re.compile(r'[^A-Za-z0-9._-]+')


def sanitize_version(raw: str) -> str:
    """A version field safe to use as a file name, or '' for an empty pin. Keeps letters, digits, dot,
    dash and underscore, and collapses every other run into one '-', so the tag `release/1.0` names an
    archive `release-1.0`.

    Keeps the case, and keeps a leading 'v'. A lowercase pass merges the tags `v1.0` and `V1.0` into one
    name, and a stripped 'v' merges `v1.0` and `1.0`. A repo may carry all three, and two sources must
    never share one archive name."""
    return _UNSAFE_IN_VERSION.sub('-', raw).strip('-') if raw else ''


# A build the compiler instrumented, which no other build may reuse. `clang` is not one of them: it
# names a compiler, and its objects are ordinary.
INSTRUMENTED_TOKENS = frozenset(_SANITIZER_SHORT_NAMES.values()) | {'cov'}
CONFIG_TOKENS = INSTRUMENTED_TOKENS | {'clang'}


def is_build_dir_of(dir_name: str, config_dir_name: str, tokens=CONFIG_TOKENS) -> bool:
    """True when `dir_name` is a build dir of the config that names itself `config_dir_name`: that dir, or
    that dir plus dep-arg tokens, because a dep the consumer added with args=[...] gets its own.

    `mama <platform> clean all` cleans ONE config, so a dir that carries another config's token (a
    sanitizer, cov, clang) is not ours even though it starts with the same platform name. A dep arg that
    happens to spell a config token reads as another config's dir and stays: a wrong delete costs more
    than a leftover dir.

    `tokens` is the set that disqualifies a dir. A host-tool search passes INSTRUMENTED_TOKENS, because
    it accepts any compiler but no instrumented objects."""
    if dir_name == config_dir_name: return True
    if not dir_name.startswith(config_dir_name + '-'): return False
    return not (set(dir_name[len(config_dir_name) + 1:].split('-')) & tokens)


def build_dir_name(config: BuildConfig, variant_suffix=None, platform_dir=None) -> str:
    """The build folder name: the platform dir, the compiler, then the variant, coarsest axis first, eg
    'linux', 'windows32', 'linux-clang-cov-asan-lgpl'. The platform maps each arch to its own dir name.
    Its primary arch uses the bare platform name, and every other arch gets a name of its own.

    The -march pin does NOT appear here, only in the artifactory archive name. The root mamafile owns the
    pin, so it is constant for a checkout and no two pins can meet in one tree. A build dir is also a path
    a project hardcodes, and renaming it breaks every consumer of that path.

    '-clang' only on a linux clang build: a shared dir means one compiler clobbers the other and then g++
    links libc++ archives. gcc keeps the bare 'linux' so existing trees do not churn, and elsewhere the
    toolset or the SDK fixes the compiler.

    `variant_suffix` defaults to the config-only variant, for a caller that names no single dep.
    `platform_dir` overrides the config's own platform dir, for the generated mama.cmake that has to name
    every arch's dir."""
    if variant_suffix is None: variant_suffix = build_variant_suffix(config)
    if platform_dir is None: platform_dir = config.platform.build_dir_name() if config.platform else 'build'
    return platform_dir + ('-clang' if (config.linux and config.clang) else '') + variant_suffix


class _HostConfigView:
    """A config as the `mama <host> build` child resolves it: the host platform, the arch of this
    machine and the compiler of this run. The child gets no coverage and no sanitizer flag."""
    coverage = False
    sanitize = None

    def __init__(self, config: BuildConfig):
        host = config.host_platform_name()
        self._config = config  # first: __getattr__ answers every field this view does not override
        self.clang = config.clang
        self.linux = host == 'linux'
        platform_class = platform_named(host)
        # The arch of this machine, never the platform default. macOS defaults to arm64, and an Intel
        # Mac cannot run an arm64 tool. An arch the platform refuses leaves the choice to the platform.
        arch = host_arch()
        self.arch = arch if arch in platform_class.supported_arches else ''
        self.platform = platform_class(self)

    def __getattr__(self, name):
        return getattr(self._config, name)  # a platform method must never trip over a missing field


def host_view(config: BuildConfig) -> _HostConfigView:
    """The config a `mama <host> build` child resolves, for a caller that needs more than one answer."""
    return _HostConfigView(config)


def host_build_dir_name(config: BuildConfig, dep_args=()) -> str:
    """The build dir name the `mama <host> build` child writes for this dep. It runs the same two
    functions the child runs, so a host-tool probe can never read a dir the child never wrote."""
    view = _HostConfigView(config)
    return build_dir_name(view, build_variant_suffix(view, dep_args))


def is_host_build(config: BuildConfig) -> bool:
    """True when this build already runs on this machine, so a host tool is the local build product.
    The arch has to RUN here, not match: a 32-bit build of a 64-bit host is still a host build. The
    variant stays out, because a sanitizer build runs the tools it built for itself."""
    platform = config.platform
    return type(platform) is type(_HostConfigView(config).platform) and platform.runs_on_host(platform.arch())
