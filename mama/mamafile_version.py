"""What version a mamafile declares, read WITHOUT running the file.

Mama names an artifactory package before it clones anything, so the download side cannot execute a
mamafile to learn its `self.version`. It reads the declaration instead. The upload side does run the
file, so the two sides agree only while the declaration is a shape a reader can resolve. This module
owns that judgement: it reports what a mamafile says, decides whether to trust it, and says so once when
it refuses.

None of this is git-specific. `Git.fetch_self_version_from_remote` fetches the text for a dep that has
no clone yet, and then asks this module what the text means. See docs/roadmap-target-version.md."""

from __future__ import annotations
import ast, os, re
from typing import NamedTuple

from .utils.system import warning
from .util import read_text_from


class VersionScan(NamedTuple):
    """What a mamafile declares about `self.version`. See `scan_mamafile`."""
    value: str      # the single resolvable string, or '' when there is not exactly one
    literals: int   # how many resolvable `self.version = ...` assignments the file holds
    computed: bool  # an assignment whose value no reader can resolve without running the file


def scan_mamafile(mamafile_text: str) -> VersionScan:
    """Report every `self.version` assignment a mamafile makes, WITHOUT running it.

    Only one shape is trustworthy: exactly one string this reader can resolve. Two of them mean the value
    depends on which branch runs, and a computed value stays invisible. In both shapes the reader would
    return a name the UPLOAD side never publishes. So it reports what it saw, and `trusted_version`
    refuses.

    Parses the file rather than grepping it. `ast.parse` costs about 0.14ms on a real mamafile, on a path
    that already spends 100ms or more on the network. It is also EXACT. A line scan counts a docstring
    that documents `self.version` as an assignment, then refuses the real pin next to it. The line scan
    stays as the fallback for a mamafile this Python cannot parse."""
    try:
        return _scan_ast(ast.parse(mamafile_text))
    except (SyntaxError, ValueError):
        return _scan_lines(mamafile_text)


def trusted_version(dep, mamafile_text: str, source: str) -> str:
    """The pinned version when the mamafile states it in the ONE shape both sides can agree on. Else '',
    so the dep names itself by commit hash on the download side AND the upload side. Warns once per dep
    on a shape it refuses, because the alternative is a silent permanent cache miss."""
    scan = scan_mamafile(mamafile_text)
    if scan.literals == 1 and not scan.computed:
        return scan.value
    if scan.literals or scan.computed:
        reason = 'assigns self.version more than once' if scan.literals > 1 else \
                 'computes self.version instead of assigning a literal'
        _warn_unusable(dep, source, reason)
    return ''


def pinned_version(dep) -> str:
    """The version the dep's own mamafile pins, read from disk without executing it. A mamafile may set
    the version in any method, and none of them run on a download probe. They run on the upload side,
    where the value renames the archive.

    Pre-clone the mamafile is on disk only for a parent-repo override (`dep.mamafile`), which is how a
    consumer names a third-party package. Post-clone it is also in the dep's own tree. Returns '' when
    the dep is unpinned, when the mamafile is not on disk yet, or when `trusted_version` refuses it."""
    path = dep.mamafile_path()
    if path and os.path.exists(path):
        try:
            return trusted_version(dep, read_text_from(path), path)
        except OSError:
            return ''
    return ''


def _scan_ast(tree) -> VersionScan:
    """The exact scan: every real assignment to `self.version`, and nothing that merely mentions it."""
    constants = _module_string_constants(tree)
    literals, computed = [], False
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign): targets, value = node.targets, node.value
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)): targets, value = [node.target], node.value
        else: continue
        if not any(isinstance(t, ast.Attribute) and t.attr == 'version'
                   and isinstance(t.value, ast.Name) and t.value.id == 'self' for t in targets):
            continue
        if isinstance(node, ast.AugAssign) or value is None: computed = True  # `+=`, or a bare `: str`
        elif isinstance(value, ast.Constant) and isinstance(value.value, str): literals.append(value.value)
        elif isinstance(value, ast.Name) and value.id in constants: literals.append(constants[value.id])
        else: computed = True
    return VersionScan(literals[0] if len(literals) == 1 else '', len(literals), computed)


def _module_string_constants(tree) -> dict:
    """Module-level `NAME = '<literal>'` bindings, which are the only names this reader can resolve. A
    name bound twice, or bound to anything but a string, resolves to nothing. The executed value would
    then depend on which binding ran last, and the reader must not guess."""
    bound = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign): continue
        literal = node.value.value if isinstance(node.value, ast.Constant) else None
        for target in node.targets:
            if isinstance(target, ast.Name):
                bound[target.id] = literal if isinstance(literal, str) and target.id not in bound else None
    return {name: value for name, value in bound.items() if value is not None}


# An assignment to self.version, anywhere on the line. `if lgpl: self.version = '8.0.1-lgpl'` is the
# shape that breaks a text reader, so it must not hide behind a line-start anchor. `[^\w.]` keeps
# `other.self.version` out, and `(?!=)` keeps a `self.version == x` comparison out.
_ASSIGN_RE = re.compile(r"""(?:^|[^\w.])self\.version\s*=(?!=)\s*(.*)""")
_LITERAL_RE = re.compile(r"""^(['"])([^'"]*)\1\s*$""")  # the WHOLE right side is one literal


def _scan_lines(mamafile_text: str) -> VersionScan:
    """The fallback for a mamafile this Python cannot parse. Line based, so it cannot tell an assignment
    from a docstring that quotes one. It also reads a multi-line literal as computed."""
    literals, computed = [], False
    for line in mamafile_text.splitlines():
        m = _ASSIGN_RE.search(line.split('#', 1)[0])  # a commented-out line assigns nothing
        if not m: continue
        literal = _LITERAL_RE.match(m.group(1).strip())
        if literal: literals.append(literal.group(2))
        else: computed = True
    return VersionScan(literals[0] if len(literals) == 1 else '', len(literals), computed)


def _warn_unusable(dep, source: str, reason: str):
    """One warning per dep per run. Both readers reach the same mamafile, and a warning repeated per
    probe teaches the reader to skip it."""
    if getattr(dep, 'warned_bad_version', False) or not dep.config.print: return
    dep.warned_bad_version = True
    warning(f'  - Target {dep.name: <16} {os.path.basename(source)} {reason}, so mama cannot read it ' +
            'before the clone. This package uses the commit hash instead. Use one raw string literal.')
