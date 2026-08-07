"""Remove published archives from the artifactory FTP.

This module deletes, so every path through it lists what it found and asks before it acts. It also
drops the local copy of anything it removes. A machine that cleans the server and keeps its own cache
would go on serving the exact package nobody else can get."""
from __future__ import annotations
from typing import List, TYPE_CHECKING
import os

from .artifactory import artifactory_ftp_login, artifactory_sanitize_url
from .utils.fileio import remove_tree
from .utils.paths import path_join, has_shim_marker
from .utils.progress import get_file_size_str
from .utils.system import Color, console, error, is_headless, warning

if TYPE_CHECKING:
    from .build_target import BuildTarget

# name, platform, os_major, compiler, arch and build_type all come before the version in an archive
# name. Only the target name can hold a `-`, and the FTP dir already tells us that one.
_FIELDS_BEFORE_VERSION = 5

_MONTHS = ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec')

DEFAULT_KEEP = 20  # how many versions `prune-old` leaves behind


class Archive:
    """One published archive: what it is called, when it landed and how big it is."""
    def __init__(self, filename: str, modify: str, size: int):
        self.filename = filename
        self.modify = modify  # 'YYYYMMDDHHMMSS' as the FTP reports it, so a plain string sorts by time
        self.size = size

    def date(self) -> str:
        """The upload date as `2026-Aug-07`, or '?' when the server reported nothing usable."""
        if len(self.modify) < 8 or not self.modify[:8].isdigit(): return '?'
        month = int(self.modify[4:6])
        if not 1 <= month <= 12: return '?'
        return f'{self.modify[0:4]}-{_MONTHS[month - 1]}-{self.modify[6:8]}'

    def __repr__(self): return f'Archive {self.filename} {self.date()} {get_file_size_str(self.size)}'


def archive_version(target_name: str, filename: str) -> str:
    """The version an archive name carries, or '' when the name does not parse.

    A variant token sits between the build type and the version, and nothing in the name marks where
    one ends. So a variant build reads as a version of its own, which keeps its history separate."""
    stem = filename[:-4] if filename.endswith('.zip') else filename
    if not stem.startswith(f'{target_name}-'): return ''
    # a name with too few fields leaves nothing after the slice, which is the '' this promises
    return '-'.join(stem[len(target_name) + 1:].split('-')[_FIELDS_BEFORE_VERSION:])


def group_by_version(target_name: str, archives: List[Archive]) -> dict:
    """{version: [archive]} for every archive whose name parses. An unparseable name is left alone,
    because a file mama cannot name is a file mama must not delete."""
    groups = {}
    for archive in archives:
        version = archive_version(target_name, archive.filename)
        if version: groups.setdefault(version, []).append(archive)
    return groups


def newest_first(groups: dict) -> List[str]:
    """Versions ordered by their newest upload. A version is as fresh as its freshest archive, so a
    rebuild of an old commit keeps that version alive.

    A server that refuses MDTM reports no date at all. An undated version sorts newest, so a prune
    never deletes what it cannot date."""
    def freshness(version): return max(a.modify or '9' * 14 for a in groups[version])
    return sorted(groups, key=freshness, reverse=True)


def select(target_name: str, archives: List[Archive], selector: str, keep: int = DEFAULT_KEEP) -> List[Archive]:
    """The archives one selector names.
    selector: an explicit version, `prune-all` for every version, or `prune-old` for all but `keep`"""
    if selector == 'prune-all':
        return [a for a in archives if archive_version(target_name, a.filename)]
    groups = group_by_version(target_name, archives)
    if selector == 'prune-old':
        doomed = newest_first(groups)[keep:]
        return [a for v in doomed for a in groups[v]]
    return groups.get(selector, [])


def connect(config):
    """An authenticated FTP session, through the same login the upload uses."""
    import ftplib  # deferred: it pulls ssl, which costs about 21ms of every mama start
    ftp = ftplib.FTP_TLS()
    artifactory_ftp_login(ftp, config, artifactory_sanitize_url(config.artifactory_ftp))
    return ftp


def list_archives(ftp, target_name: str) -> List[Archive]:
    """Every archive published under `target_name`, with its upload time and size.

    MLSD answers both facts in one round trip. A server without it costs one MDTM and one SIZE per
    file, which is why the fallback is not the first choice."""
    try:
        return [Archive(name, facts.get('modify', ''), int(facts.get('size', 0)))
                for name, facts in ftp.mlsd(target_name) if name.endswith('.zip')]
    except Exception:
        return _list_archives_without_mlsd(ftp, target_name)


def _list_archives_without_mlsd(ftp, target_name: str) -> List[Archive]:
    archives = []
    for path in ftp.nlst(target_name):
        name = os.path.basename(path)
        if not name.endswith('.zip'): continue
        modify, size = '', 0
        try: modify = ftp.voidcmd(f'MDTM {target_name}/{name}')[4:].strip()
        except Exception: pass
        try: size = ftp.size(f'{target_name}/{name}') or 0
        except Exception: pass
        archives.append(Archive(name, modify, size))
    return archives


def delete_archives(ftp, target_name: str, archives: List[Archive]) -> List[Archive]:
    """Delete each archive and return the ones that went. A failure reports itself and the rest
    continue, because a half-cleaned server beats a stop at the first locked file.

    The caller purges the local copy of what this returns, never of what it was asked to delete. A
    zip still on the server must keep its local copy, or the next build re-downloads it for nothing."""
    deleted = []
    for archive in archives:
        try:
            ftp.delete(f'{target_name}/{archive.filename}')
            deleted.append(archive)
        except Exception as e:
            error(f'  - {target_name: <16} UNPUBLISH failed for {archive.filename}: {e}')
    return deleted


def describe_run(doomed: dict, url: str) -> str:
    """The listing a user reads before confirming, over every target of the run.

    Every line carries the date and the size, because those are what tell a stale package from one
    somebody still needs. Oldest first, so the tail of the list is what a prune is about to take."""
    every = [a for archives in doomed.values() for a in archives]
    width = max(len(a.filename) for a in every)
    lines = [f'  {a.filename: <{width}}  {a.date(): >11}  {get_file_size_str(a.size): >9}'
             for a in sorted(every, key=lambda a: (a.modify, a.filename))]
    versions = sum(len(group_by_version(t.name, a)) for t, a in doomed.items())
    return '\n'.join(lines) + f'\n  {len(every)} archive(s), {versions} version(s), ' + \
           f'{len(doomed)} target(s) on {url}'


def local_copies(target: BuildTarget, archives: List[Archive]) -> List[str]:
    """The cached zip of each archive that this machine holds, plus the build dir of a shim that serves
    one of them. A shim of a version this run kept stays, because its package is still on the server."""
    dep = target.dep
    names = {a.filename for a in archives}
    paths = [p for p in (path_join(dep.dep_dir, n) for n in names) if os.path.exists(p)]
    shim_archive = dep.read_shim_marker().get('archive', '') if has_shim_marker(dep.build_dir) else ''
    if shim_archive and f'{shim_archive}.zip' in names: paths.append(dep.build_dir)
    return paths


def purge_local(target: BuildTarget, archives: List[Archive]) -> int:
    """Remove every local copy of the deleted archives. Returns how many paths went."""
    removed = 0
    for path in local_copies(target, archives):
        try:
            if os.path.isdir(path): remove_tree(path)
            else: os.remove(path)
            removed += 1
        except OSError as e:
            warning(f'  - {target.name: <16} UNPUBLISH could not remove {path}: {e}')
    return removed


def _confirm(listing: str, assume_yes: bool) -> bool:
    """Show what will go and ask. A headless run cannot answer a prompt, so it refuses unless the
    command already said `yes`. Refusing beats hanging a CI job forever on stdin."""
    warning('  UNPUBLISH will delete these archives, and this cannot be undone:')
    console(listing)
    if assume_yes:
        console('  The command said `yes`, so no prompt.')
        return True
    if is_headless():
        error('  - UNPUBLISH refused: no terminal to confirm on. Add `yes` to the command to proceed.')
        return False
    try:
        answer = input('  Delete them? [y/N] ').strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer in ('y', 'yes')


def in_scope(target) -> bool:
    """True when the target the USER typed names this dep.

    It reads `user_target`, not `config.target`. An `update` rewrites `config.target` to `all`, and an
    unpublish that followed it would delete every version of every dep the graph holds."""
    named = target.config.user_target
    if not named: return target.dep.is_root
    if named.lower() == 'all': return True
    return named.lower() == target.name.lower()


def _resolve(target, selector: str) -> str:
    """`current` becomes the version this checkout names. Every other selector passes through."""
    if selector != 'current': return selector
    from .artifactory import artifactory_archive_name  # local import: avoid a cycle
    archive = artifactory_archive_name(target)
    version = archive_version(target.name, archive) if archive else ''
    if not version:
        raise RuntimeError(f'unpublish=current cannot name a version for {target.name}. ' + \
                           'Pass the version instead, as unpublish=<version>.')
    return version


def unpublish_run(targets, config) -> int:
    """Delete the archives this run selects, across every target it names, behind ONE prompt.

    One prompt and one FTP session for the whole run: `mama all unpublish=prune-old` would otherwise
    ask thirty times, and nobody reads the thirtieth question. Returns how many archives went."""
    if not config.artifactory_ftp:
        raise RuntimeError('Unpublish failed: artifactory_ftp not set by config.set_artifactory_ftp()')
    url = artifactory_sanitize_url(config.artifactory_ftp)
    # `is None`, not `or`: `prune-old=0` means keep nothing, and `or` would read it as the default
    keep = DEFAULT_KEEP if config.unpublish_keep is None else config.unpublish_keep

    doomed = {}  # target -> [archive], so the listing can group by target and the purge can follow
    ftp = connect(config)
    try:
        for target in targets:
            if target.dep.dep_source.is_pkg:
                warning(f'  - Target {target.name: <16} UNPUBLISH skipped (artifactory pkg is read-only)')
                continue
            picked = select(target.name, list_archives(ftp, target.name), _resolve(target, config.unpublish), keep)
            if picked: doomed[target] = picked
        if not doomed:
            console(f'  Nothing to unpublish for `{config.unpublish}` on {url}')
            return 0
        if not _confirm(describe_run(doomed, url), config.assume_yes):
            console('  UNPUBLISH cancelled')
            return 0
        gone = {t: delete_archives(ftp, t.name, a) for t, a in doomed.items()}
    finally:
        try: ftp.quit()
        except Exception: pass

    deleted = sum(len(a) for a in gone.values())
    removed = sum(purge_local(t, a) for t, a in gone.items() if a)
    console(f'  UNPUBLISHED {deleted} archive(s) across {len(doomed)} target(s), ' + \
            f'and removed {removed} local copy(s)', color=Color.GREEN)
    return deleted
