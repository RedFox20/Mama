from __future__ import annotations

import json, os, re, tempfile
from dataclasses import dataclass

from .types.git import Git, canonical_git_remote, parse_git_url, same_git_remote
from .utils.fileio import read_text_from
from .utils.paths import normalized_path, path_join
from .utils.sub_process import execute_piped
from .utils.system import console


LOCK_FILENAME = 'mama.lock'
LOCK_FORMAT = 1
_COMMIT_RE = re.compile(r'^[0-9a-fA-F]{7,40}$')


@dataclass(frozen=True)
class LockSelector:
    kind: str
    value: str


@dataclass(frozen=True)
class LockEntry:
    name: str
    repository: str
    selector: LockSelector
    commit: str

    def json(self) -> dict:
        return {'name': self.name, 'repository': self.repository,
                'selector': {'kind': self.selector.kind, 'value': self.selector.value}, 'commit': self.commit}


@dataclass(frozen=True)
class _Declaration:
    name: str
    repository: str
    selector: LockSelector
    mamafile: str
    version_suffix: str


def selector_for(git: Git) -> LockSelector:
    if git.branch: return LockSelector('branch', git.branch)
    if git.tag and Git.is_hex_string(git.tag): return LockSelector('commit', git.tag)
    if git.tag: return LockSelector('tag', git.tag)
    return LockSelector('head', '')


def _entry(raw: dict) -> LockEntry:
    try:
        selector = raw['selector']
        entry = LockEntry(raw['name'], raw['repository'],
                          LockSelector(selector['kind'], selector.get('value', '')), raw['commit'].lower())
    except (KeyError, AttributeError, TypeError) as error:
        raise RuntimeError(f'{LOCK_FILENAME} has an invalid dependency entry: {raw!r}') from error
    if not entry.name or not entry.repository:
        raise RuntimeError(f'{LOCK_FILENAME} dependency names and repositories must not be empty')
    if entry.selector.kind not in ('branch', 'tag', 'commit', 'head'):
        raise RuntimeError(f'{LOCK_FILENAME} has unknown selector kind {entry.selector.kind!r} for {entry.name}')
    if entry.selector.kind != 'head' and not entry.selector.value:
        raise RuntimeError(f'{LOCK_FILENAME} has an empty {entry.selector.kind} selector for {entry.name}')
    if not re.fullmatch(r'[0-9a-f]{40}', entry.commit):
        raise RuntimeError(f'{LOCK_FILENAME} has an invalid commit for {entry.name}: {entry.commit!r}')
    return entry


def read_lock(source_dir: str, required: bool = False):
    path = path_join(source_dir, LOCK_FILENAME)
    if not os.path.exists(path):
        if required: raise RuntimeError(f'{LOCK_FILENAME} does not exist. Run `mama lock platforms=<platforms>` first')
        return None
    try:
        data = json.loads(read_text_from(path))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f'Could not read {path}: {error}') from error
    if not isinstance(data, dict): raise RuntimeError(f'{LOCK_FILENAME} root must be an object')
    if data.get('format') != LOCK_FORMAT:
        raise RuntimeError(f'{LOCK_FILENAME} needs format {LOCK_FORMAT}, got {data.get("format")!r}')
    platforms = data.get('platforms')
    dependencies = data.get('dependencies')
    if not isinstance(platforms, list) or not all(isinstance(platform, str) and platform for platform in platforms):
        raise RuntimeError(f'{LOCK_FILENAME} platforms must be a list of names')
    if not isinstance(dependencies, list): raise RuntimeError(f'{LOCK_FILENAME} dependencies must be a list')
    entries = {}
    for raw in dependencies:
        entry = _entry(raw)
        if entry.name in entries: raise RuntimeError(f'{LOCK_FILENAME} contains {entry.name} more than once')
        entries[entry.name] = entry
    return DependencyLock(path, tuple(platforms), entries)


class DependencyLock:
    def __init__(self, path: str, platforms: tuple[str, ...], entries: dict[str, LockEntry]):
        self.path = path
        self.platforms = platforms
        self.entries = entries
        self._declarations = {}

    def validate_platform(self, platform: str):
        if platform not in self.platforms:
            raise RuntimeError(f'{LOCK_FILENAME} does not cover {platform}. Regenerate it with platforms including {platform}')

    def apply(self, git: Git, parent=None):
        declaration = _declaration(git, parent=parent)
        _remember_declaration(self._declarations, declaration)
        entry = self.entries.get(git.name)
        if not entry: raise RuntimeError(f'{LOCK_FILENAME} has no entry for active Git dependency {git.name}')
        _validate_entry(entry, declaration)
        git.locked_commit = entry.commit


def _declaration(git: Git, parent=None) -> _Declaration:
    mamafile = ''
    if git.mamafile:
        if parent:
            mamafile = parent.path_relative_to_us(git.mamafile)
        else:
            mamafile = git.mamafile
    # BuildDependency applies the URL override later. The lock keeps the machine-independent
    # repository that mamafile.py declares.
    return _Declaration(git.name, canonical_git_remote(git.url), selector_for(git), mamafile,
                        git.version_suffix or '')


def _remember_declaration(seen: dict[str, _Declaration], declaration: _Declaration):
    previous = seen.get(declaration.name)
    if previous and previous != declaration:
        raise RuntimeError(f'Git dependency {declaration.name} has conflicting declarations: {previous} and {declaration}')
    seen[declaration.name] = declaration


def _validate_entry(entry: LockEntry, declaration: _Declaration):
    if entry.repository != declaration.repository:
        raise RuntimeError(f'{LOCK_FILENAME} repository for {entry.name} is {entry.repository}, '
                           f'but mamafile.py declares {declaration.repository}. Run `mama lock`')
    if entry.selector != declaration.selector:
        old = f'{entry.selector.kind}={entry.selector.value}'
        new = f'{declaration.selector.kind}={declaration.selector.value}'
        raise RuntimeError(f'{LOCK_FILENAME} selector for {entry.name} is {old}, '
                           f'but mamafile.py declares {new}. Run `mama lock`')


def _validate_checkout(dep, git: Git):
    if git._is_repo_broken(dep): raise RuntimeError(f'Cannot lock {git.name}: unusable Git repository at {dep.src_dir}')

    origin = execute_piped(['git', 'remote', 'get-url', 'origin'], cwd=dep.src_dir, throw=False) or ''
    # Clone URLs are relative to Mama's cwd; Git reads a checkout's relative origin from dep.src_dir.
    cwd = os.getcwd()
    if parse_git_url(origin) is None and not origin.lower().startswith('file://'):
        origin = normalized_path(os.path.join(dep.src_dir, origin))
    declared = git.url
    if parse_git_url(declared) is None and not declared.lower().startswith('file://'):
        declared = normalized_path(os.path.join(cwd, declared))
    if not same_git_remote(origin, declared):
        raise RuntimeError(f'Cannot lock {git.name}: checkout origin {origin!r} does not match '
                           f'effective repository {declared!r}')


def _remote_selector_commit(dep, git: Git, selector: LockSelector) -> str:
    if selector.kind == 'branch':
        remote_ref = f'refs/remotes/origin/{selector.value}'
        git.run_git(dep, f'fetch origin +refs/heads/{selector.value}:{remote_ref}')
        commit = git._full_local_commit(dep.src_dir, remote_ref)
    elif selector.kind == 'tag':
        tag_ref = f'refs/tags/{selector.value}'
        git.run_git(dep, f'fetch --force origin {tag_ref}:{tag_ref}')
        commit = git._full_local_commit(dep.src_dir, tag_ref)
    elif selector.kind == 'commit':
        commit = git._full_local_commit(dep.src_dir, selector.value)
        if commit:
            git.run_git(dep, f'fetch origin {commit}')
            if git._full_local_commit(dep.src_dir, 'FETCH_HEAD') != commit: commit = ''
    else:
        git.run_git(dep, 'fetch origin HEAD')
        commit = git._full_local_commit(dep.src_dir, 'FETCH_HEAD')

    if not re.fullmatch(r'[0-9a-f]{40}', commit):
        value = f'={selector.value}' if selector.value else ''
        raise RuntimeError(f'Cannot lock {git.name}: could not resolve remote {selector.kind}{value}')
    return commit


class LockGeneration(DependencyLock):
    def __init__(self, source_dir: str, platforms: tuple[str, ...], existing: DependencyLock | None,
                 target: str | None, commit: str | None):
        super().__init__(path_join(source_dir, LOCK_FILENAME), platforms, existing.entries if existing else {})
        self.source_dir = source_dir
        self.target = target
        self.commit = commit.lower() if commit else None
        self._resolved: dict[str, LockEntry] = {}
        self._active = set()
        self._target_seen = False
        self._refresh_subtree = set()

    def apply(self, git: Git, parent=None):
        declaration = _declaration(git, parent=parent)
        _remember_declaration(self._declarations, declaration)
        self._active.add(git.name)
        resolved = self._resolved.get(git.name)
        if resolved:
            _validate_entry(resolved, declaration)
            git.locked_commit = resolved.commit
            return

        selected = self.target is not None and git.name.lower() == self.target.lower()
        parent_name = parent.dep_source.name if parent and parent.dep_source.is_git else None
        in_refresh_subtree = parent_name in self._refresh_subtree
        if selected:
            self._target_seen = True
            self._refresh_subtree.add(git.name)
            if self.commit:
                if declaration.selector.kind not in ('branch', 'head'):
                    raise RuntimeError(f'commit= cannot override {git.name} declared with '
                                       f'{declaration.selector.kind}={declaration.selector.value}. Edit mamafile.py')
                git.locked_commit = self.commit
                git.lock_commit_override = True
            return

        entry = self.entries.get(git.name)
        if entry:
            if entry.repository == declaration.repository and entry.selector == declaration.selector:
                git.locked_commit = entry.commit
            elif self.target and not in_refresh_subtree: _validate_entry(entry, declaration)
        elif self.target and not in_refresh_subtree:
            raise RuntimeError(f'{LOCK_FILENAME} has no entry for active Git dependency {git.name}. Run `mama lock`')
        if in_refresh_subtree: self._refresh_subtree.add(git.name)

    def checkout(self, dep) -> bool:
        """Resolve and check out this declaration before its mamafile executes."""
        git: Git = dep.dep_source
        declaration = self._declarations[git.name]
        changed = git.dependency_checkout(dep)
        _validate_checkout(dep, git)
        if not git.locked_commit:
            git.locked_commit = _remote_selector_commit(dep, git, declaration.selector)
            locked_changed = git.checkout_locked_commit(dep)
            if locked_changed:
                git.update_submodules(dep, shallow=not (dep.config.unshallow or not git.shallow))
            changed = locked_changed or changed
        return changed

    def record(self, dep):
        git: Git = dep.dep_source
        commit = git._full_local_commit(dep.src_dir, 'HEAD')
        if commit != git.locked_commit:
            raise RuntimeError(f'Cannot lock {git.name}: checkout HEAD changed while loading its mamafile')
        declaration = self._declarations[git.name]
        entry = LockEntry(git.name, declaration.repository, declaration.selector, commit)
        previous = self._resolved.get(git.name)
        if previous and previous != entry:
            raise RuntimeError(f'Git dependency {git.name} resolved inconsistently: {previous} and {entry}')
        self._resolved[git.name] = entry

    def write(self):
        if self.target and not self._target_seen: raise RuntimeError(f'Git dependency {self.target!r} was not found')
        sort_name = lambda name: (name.lower(), name)
        missing = sorted(self._active - self._resolved.keys(), key=sort_name)
        if missing: raise RuntimeError(f'Could not resolve locked commits for: {", ".join(missing)}')
        dependencies = [self._resolved[name].json() for name in sorted(self._active, key=sort_name)]
        contents = json.dumps({'format': LOCK_FORMAT, 'platforms': list(self.platforms),
                               'dependencies': dependencies}, indent=2) + '\n'
        if os.path.exists(self.path) and read_text_from(self.path) == contents:
            console(f'{LOCK_FILENAME} is unchanged')
            return
        fd, incoming = tempfile.mkstemp(prefix=f'.{LOCK_FILENAME}.', dir=self.source_dir, text=True)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream: stream.write(contents)
            os.replace(incoming, self.path)
        finally:
            if os.path.exists(incoming): os.remove(incoming)
        console(f'Wrote {self.path}')


def _parse_args(args: list[str]):
    platforms = None
    commit = None
    target = None
    passthrough = []
    for arg in args:
        if arg == 'lock': continue
        if arg.startswith('platforms='):
            platforms = tuple(sorted(dict.fromkeys(filter(None, arg[10:].split(',')))))
        elif arg.startswith('commit='):
            commit = arg[7:]
        elif arg in ('verbose', 'silent', 'https-override', 'ssh-override') or arg.startswith('git_timeout='):
            passthrough.append(arg)
        elif '=' in arg or arg.startswith('-'):
            raise RuntimeError(f'Unknown lock option {arg!r}')
        elif target:
            raise RuntimeError(f'Lock accepts one dependency name, got {target!r} and {arg!r}')
        else:
            target = arg
    if commit and not target: raise RuntimeError('commit=<sha> requires a dependency name')
    if commit and not _COMMIT_RE.fullmatch(commit): raise RuntimeError('commit=<sha> needs 7 to 40 hexadecimal characters')
    if not platforms: raise RuntimeError('lock needs platforms=<comma-separated platforms>')
    return platforms, target, commit, passthrough


def run_lock(args: list[str], source_dir: str):
    from .build_config import BuildConfig
    from .build_dependency import BuildDependency
    from .dependency_chain import load_dependency_chain
    from .platforms.registry import platform_for_arg
    from .types.local_source import LocalSource
    from .utils.git_status import load_repo_status

    source_dir = normalized_path(source_dir)
    platforms, target, commit, passthrough = _parse_args(args)
    existing = read_lock(source_dir, required=True) if target else None
    if existing: platforms = tuple(sorted(set(platforms + existing.platforms)))
    for platform in platforms:
        selected = platform_for_arg(platform)
        if not selected: raise RuntimeError(f'Unknown lock platform {platform!r}')
        if platform != selected[0].name:
            raise RuntimeError(f'Lock platform {platform!r} must use canonical name {selected[0].name!r}')
    generation = LockGeneration(source_dir, platforms, existing, target, commit)

    load_repo_status(source_dir)
    for platform in platforms:
        config = BuildConfig(['update', 'all', 'noart', 'serial', platform, *passthrough])
        config.root_source_dir = source_dir
        config.lock_generation = True
        config.dependency_lock = generation
        local = LocalSource(os.path.basename(source_dir), source_dir, mamafile=None, always_build=False, args=[])
        root = BuildDependency(None, config, None, local)
        root.load()
        load_dependency_chain(root)
    generation.write()
