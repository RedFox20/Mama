from __future__ import annotations
from typing import List, TYPE_CHECKING
import os, re, sys, shutil, time, threading

from .types.dep_source import DepSource
from .types.git import Git
from .types.local_source import LocalSource
from .utils.system import Color, console, error, warning
from .utils.dir_lock import interprocess_dir_lock
from .artifactory import artifactory_fetch_and_reconfigure, try_load_artifactory_shim
from .mamafile_version import pinned_version
from .utils.fileio import read_text_from, write_text_to, read_lines_from
from .utils.paths import normalized_join, normalized_path, short_path, has_shim_marker, \
                         has_source_content, forward_slashes, MAMA_SHIM_FILENAME
from . import build_names
from .parse_mamafile import parse_mamafile, update_mamafile_tag, update_cmakelists_tag
import mama.package as package


if TYPE_CHECKING:
    from .build_config import BuildConfig
    from .build_target import BuildTarget


# Backstop for the cross-process dep-dir lock: the git_timeout kills a stalled clone and releases the
# lock first. On expiry the waiter proceeds without the lock, so a stuck holder cannot block a build forever.
_LOAD_LOCK_TIMEOUT_SEC = 300


######################################################################################


MAMA_CMAKE = 'mama.cmake'
# every span that holds no command: a bracket comment or argument of any equals-sign depth, a quoted
# argument, a line comment, and an escape pair, whose `(` or `)` cmake reads as a plain character
_CMAKE_SKIP = re.compile(r'#\[(=*)\[.*?\]\1\]|\[(=*)\[.*?\]\2\]|"(?:\\.|[^"\\])*"|#[^\n]*|\\.', re.S)
# a command invocation: a name, then the paren that opens its argument list. `first_cmake_arg`
# below names every argument form the scan reads, which is a deliberately small set
_CMAKE_COMMAND = re.compile(r'(?<!\w)([A-Za-z_]\w*)\s*\(')
# the first argument, past any whitespace and comment. A bracket comment comes first, so `#[[` never
# reads as a line comment and eats the argument behind it
_CMAKE_FIRST_ARG = re.compile(r'(?:\s|\#\[(?P<eq>=*)\[.*?\](?P=eq)\]|\#[^\n]*)*'
                              r'(?:"(?P<quoted>(?:\\.|[^"\\])*)"|(?P<plain>[^\s()\#"]+))', re.S)
# cmake dir variables: the dir of the file that names them, the nearest project(), and the top dir
_CMAKE_CURRENT_DIR_VARS = ('${CMAKE_CURRENT_LIST_DIR}', '${CMAKE_CURRENT_SOURCE_DIR}')
_CMAKE_PROJECT_DIR_VAR = '${PROJECT_SOURCE_DIR}'
_CMAKE_TOP_DIR_VAR = '${CMAKE_SOURCE_DIR}'
# cmake names a variable three ways. A `$` outside them is ordinary content of an argument
_CMAKE_VAR_REF = re.compile(r'\$(?:ENV|CACHE)?\{')


def first_cmake_arg(args: str, pos: int = 0) -> str:
    """The first argument of the cmake command whose `(` ends at `pos`, or '' when it names none.

    The scan reads THREE forms, and no others:
        add_subdirectory(src)              a plain word, up to the first space, paren, quote or `#`
        add_subdirectory("src dir")        a quoted string, taken between the quotes as it is written
        include(${CMAKE_SOURCE_DIR}/x)     either form, holding a dir variable `expand_cmake_dirs` knows

    A regex does not parse the cmake language, and mama does not try. A bracket argument, an escape
    sequence, a `;` list and a make-style `$(VAR)` all read as one plain word. The scan then names a
    path cmake never reads, cmake reports the file it wanted, and one `include(mama.cmake)` answers it.
    """
    match = _CMAKE_FIRST_ARG.match(args, pos)
    return (match.group('quoted') or match.group('plain') or '') if match else ''


def has_unknown_cmake_var(arg: str) -> bool:
    """True when the argument names a variable mama does not expand, such as $ENV{} or a project one.
    It tests before substitution, because a checkout path may hold a `$` of its own."""
    for var in (*_CMAKE_CURRENT_DIR_VARS, _CMAKE_PROJECT_DIR_VAR, _CMAKE_TOP_DIR_VAR):
        arg = arg.replace(var, '')
    return _CMAKE_VAR_REF.search(arg) is not None


def expand_cmake_dirs(arg: str, current_dir: str, project_dir: str, top_dir: str) -> str:
    """The argument with every cmake dir variable mama knows replaced by the dir it names."""
    for var in _CMAKE_CURRENT_DIR_VARS: arg = arg.replace(var, current_dir)
    return arg.replace(_CMAKE_PROJECT_DIR_VAR, project_dir).replace(_CMAKE_TOP_DIR_VAR, top_dir)


def scan_cmake_commands(cmakelists: str, commands: tuple) -> list:
    """(command, first argument) for every named command, in source order, so a caller can follow a
    variable a command rebinds. `depth` counts the parens still open, so a command cmake hands to
    another as text names nothing. 'surrogateescape' keeps a byte mama cannot decode, which still has
    to reach the path it writes."""
    raw = ''.join(read_lines_from(cmakelists, errors='surrogateescape'))
    # blanks of the same length hide every comment and quoted span, so the structure reads from `text`
    # and the argument still reads from `raw` at the same index
    text = _CMAKE_SKIP.sub(lambda span: ' ' * len(span.group()), raw)
    found, depth, pos = [], 0, 0
    for match in _CMAKE_COMMAND.finditer(text):
        depth += text.count('(', pos, match.start()) - text.count(')', pos, match.start())
        nested = depth > 0
        pos, depth = match.end(), depth + 1   # the paren this command opens
        name = match.group(1).lower()
        if not nested and name in commands: found.append((name, first_cmake_arg(raw, match.end())))
    return found


def find_mama_cmake_includes(cmakelists: str, source_dir: str) -> list:
    """(dir, project_dir, argument) for every `include()` naming the `mama.cmake` proxy, in every file
    cmake reads from `cmakelists`, which `source_dir` holds. The scan follows `add_subdirectory()`, and
    an argument naming an unknown variable stops that branch. `project_dir` is the dir of the last
    `project()` ABOVE the include, which is what `PROJECT_SOURCE_DIR` expands to there."""
    pending, seen, found = [(cmakelists, source_dir, source_dir, ())], set(), []
    while pending:
        path, cwd, project_dir, ancestors = pending.pop(0)
        # cmake reads one source dir once per project scope, and two symlink aliases are two source dirs
        key = (cwd, project_dir)
        if key in seen or not os.path.exists(path): continue
        seen.add(key)
        real = os.path.realpath(cwd)
        if real in ancestors: continue   # a symlink that names an ancestor would walk that chain forever
        ancestors += (real,)
        for name, arg in scan_cmake_commands(path, ('include', 'add_subdirectory', 'project')):
            if name == 'project':
                project_dir = cwd
            elif name == 'include':
                # the basename must match, or a write would replace a real module such as grandmama.cmake
                if os.path.basename(forward_slashes(arg)).lower() == MAMA_CMAKE:
                    found.append((cwd, project_dir, arg))
            elif not has_unknown_cmake_var(arg):   # mama expands no variable a CMakeLists.txt sets
                sub = normalized_join(cwd, expand_cmake_dirs(arg, cwd, project_dir, source_dir))
                pending.append((normalized_join(sub, 'CMakeLists.txt'), sub, project_dir, ancestors))
    return found


def read_shim_marker_at(build_dir: str) -> dict:
    """Shim metadata from ANY build dir, or an empty dict when it holds no marker. A dep owns one build
    dir, and an unpublish reads the dirs of the other platforms too.
    Keys: name, url, branch, tag, hash, archive."""
    result = {}
    path = normalized_join(build_dir, MAMA_SHIM_FILENAME)
    if not os.path.exists(path):
        return result
    for line in read_lines_from(path):
        line = line.rstrip()
        if not line or line == 'shim 1':
            continue
        key, _, value = line.partition(' ')
        result[key] = value
    return result


class BuildDependency:
    def __init__(self, parent:BuildDependency, config:BuildConfig,
                 workspace:str, dep_source:DepSource):
        self.config = config
        self.workspace = workspace
        self.mamafile = None
        self.target: BuildTarget = None
        self.target_args = []
        self.always_build = False
        self.should_rebuild = False
        self.nothing_to_build = False
        self.already_loaded = False
        self.already_executed = False
        self.currently_loading = False
        self.load_deferred = False # a targeted load skipped the clone fallback of a dep outside the target
        self.clone_revived = False # revive_deferred_load ran, so the next load must clone
        self.skimming = False   # a skim runs the hooks right now, so every dep path reads as unresolved
        self.did_skim = False   # settings() and dependencies() already ran, so the load must not repeat them
        self.load_action = 'check'  # what load() did, for the display: check|clone|pulling|local|artifactory
        self.phase_times = {}  # 'load'|'configure'|'build' -> wall seconds, for the `buildstats` breakdown
        self._load_lock = threading.Lock()  # serializes concurrent load() of THIS dep (parallel_load)
        self.from_artifactory = False # True when this dep loaded from artifactory
        self.artifactory_archive = '' # the package it unpacked, so a listing can name the source of the exports
        self.did_check_artifactory = False # True when the artifactory check already ran, so skip it
        self._is_shim_cache = None # tri-state cache for is_artifactory_shim()
        self.is_root = parent is None # a root dep always builds
        self.children: List[BuildDependency] = []
        self.product_sources = []
        self.flattened_deps: List[BuildDependency] = [] # flat dependencies only, nothing else

        self.src_dir = None # source directory that holds the code
        self.dep_dir = None # dependency dir that holds the platform build dirs
        self.build_dir_name = None # this dep's platform+variant dir name, eg 'linux-asan-lgpl'
        self.build_dir = None # {dep_dir}/{build_dir_name}
        self.dep_source = dep_source
        self.name = dep_source.name

        if dep_source.is_git:
            git:Git = dep_source
            git.apply_url_override(config)
            self.mamafile = git.mamafile # git.mamafile is the relative path
            if parent:
                self.mamafile = parent.get_mamafile_path_relative_to_us(self.name, git.mamafile)
            self._add_args(git.args)
            self._update_dep_name_and_dirs(self.name)
            self.src_dir = normalized_join(self.dep_dir, self.name)
        elif dep_source.is_pkg:
            if not config.artifactory_ftp:
                raise RuntimeError(f'add_artifactory_pkg({self.name}) failed because config.artifactory_ftp is not set!')
            self.src_dir = None # an artifactory package has no src_dir
            self.create_build_target()
        elif dep_source.is_src:
            src:LocalSource = dep_source
            self.mamafile = src.mamafile
            self._add_args(src.args)
            self.always_build = src.always_build

            if parent:
                self.mamafile = parent.get_mamafile_path_relative_to_us(self.name, src.mamafile)
                self.src_dir = parent.path_relative_to_us(src.rel_path)
            else:
                self.src_dir = normalized_path(src.rel_path)

            if self.mamafile and not os.path.exists(self.mamafile):
                raise OSError(f'{self.name} mamafile path does not exist: {self.mamafile}')
            if not os.path.exists(self.src_dir):
                raise OSError(f'{self.name} source dir does not exist: {self.src_dir}')

            self.create_build_target()
        else:
            raise RuntimeError(f'{self.name} src or git or pkg not configured. Specify at least one.')


    def __str__(self): return f'BuildDependency {self.name} {self.dep_source}'
    def __repr__(self): return f'BuildDependency {self.name} {self.dep_source}'


    def _add_args(self, args):
        if args: # skip empty args
            for arg in args:
                if arg:
                    self.target_args.append(arg)


    def update_existing_dependency(self, dep_source: DepSource):
        if dep_source.is_git or dep_source.is_src:
            self._add_args(dep_source.args)
            self._update_dep_name_and_dirs(self.name)  # new args -> new variant suffix -> new build dir
            if self.target:
                self.target._set_args(self.target_args)


    def add_child(self, dep_source: DepSource) -> BuildDependency:
        """
        Add a child dependency. Under parallel_load two parents can add the same child concurrently, and
        the registry lock makes lookup and creation atomic, so a shared (diamond) dep resolves to one instance.
        dep_source: the DepSource that names the child
        """
        with self.config.dep_registry_lock:
            dep = self.config.loaded_dependencies.get(dep_source.name)
            if dep:
                dep.update_existing_dependency(dep_source)
            else:
                dep = BuildDependency(self, self.config, self.workspace, dep_source)
                self.config.loaded_dependencies[dep_source.name] = dep
                if self.config.verbose:
                    console(f'  - Target {self.name: <16} ADD {dep}', color=Color.BLUE)

            if dep in self.children:
                raise RuntimeError(f"BuildTarget {self.name} add dependency '{dep.name}'"\
                                    " failed because it has already been added")

            self.children.append(dep)
            return dep


    def add_children(self, dep_sources):
        """Add papa.txt children, skipping any child already present: a shim probe and a post-clean re-extract
        report the same list. The duplicate raise in add_child must stay for a real mamafile double-declare."""
        existing = {c.name for c in self.children}
        for dep_source in dep_sources:
            if dep_source.name not in existing: self.add_child(dep_source)


    def get_children(self) -> List[BuildDependency]:
        """ Return the resolved child dependencies. """
        if self.children is None:
            raise RuntimeError(f'Target {self.name} child dependencies unresolved')
        return self.children


    def _update_dep_name_and_dirs(self, name):
        self.name = name
        dep_name = name
        # A branch or tag in the dep name complicates the package system and adds little value, so dep_name stays plain.
        # The build dir and the artifactory archive name both read this variant suffix, so a build and its
        # uploaded package always agree. A second parent that adds more args recomputes it (update_existing_dependency).
        self.variant_suffix = build_names.build_variant_suffix(self.config, self.target_args)
        self.dep_dir = normalized_join(self.config.workspaces_root, self.workspace, dep_name)
        self.build_dir_name = build_names.build_dir_name(self.config, self.variant_suffix)
        self.build_dir = normalized_join(self.dep_dir, self.build_dir_name)


    def has_build_files(self):
        return self.build_file_exists('CMakeCache.txt') \
            or self.build_file_exists('Makefile')


    def is_first_time_build(self):
        def first_time_build():
            return not self.build_file_exists('mamafile_tag') \
                and not self.build_file_exists('CMakeCache.txt')
        # a targeted rebuild must not send every other dep through the artifactory probe again
        rebuild_all = self.config.rebuild and self.config.no_specific_target()
        return rebuild_all or first_time_build()


    def exported_libs_file(self):
        return self.build_dir + '/mama_exported_libs'


    def papa_package_file(self):
        return self.build_dir + '/papa.txt'


    def load_build_products(self, target):
        """ Load the build products that the last build recorded. """
        loaded_deps = read_lines_from(self.exported_libs_file())
        if loaded_deps:
            package.set_export_libs_and_products(target, loaded_deps)


    def save_exports_as_dependencies(self, exports):
        write_text_to(self.exported_libs_file(), '\n'.join(exports))


    def has_usable_artifacts(self) -> bool:
        """True if a dependent can link or include against something on disk. build_products carries the
        exports of the last build, so a custom build() target with no CMakeCache still counts as built."""
        if self.from_artifactory or self.nothing_to_build or self.is_artifactory_shim(): return True
        if self.target is None: return self.has_build_files()  # load failed/never ran: judge by the build dir
        if self.find_first_missing_build_product(): return False
        return bool(self.target.build_products) or self.has_build_files()


    def find_first_missing_build_product(self):
        for depfile in self.target.build_products:
            if not os.path.exists(depfile):
                return depfile
        return None


    def source_dir_exists(self):
        return self.src_dir and os.path.exists(self.src_dir)


    def build_dir_exists(self):
        return os.path.exists(self.build_dir)


    def mama_shim_file(self) -> str:
        """ Marker file path that identifies this dep as an artifactory shim. """
        return normalized_join(self.build_dir, MAMA_SHIM_FILENAME)


    def is_artifactory_shim(self) -> bool:
        """True if this dep loaded from artifactory without a git clone.
        Cached: only write_shim_marker, remove_shim_marker and dirty() change the state."""
        if self._is_shim_cache is None:
            self._is_shim_cache = (self.dep_source.is_git and os.path.exists(self.mama_shim_file())
                                   and not self.is_real_clone())
        return self._is_shim_cache


    def is_real_clone(self) -> bool:
        """ True if this dep has an actual git working tree on disk. """
        return self.src_dir is not None and os.path.exists(f'{self.src_dir}/.git')


    def write_shim_marker(self, archive_name: str, commit_hash: str):
        """ Write the shim metadata, so a later run can identify the shim and its backing archive. """
        git: Git = self.dep_source
        lines = [
            'shim 1',
            f'name {self.name}',
            f'url {git.url}',
            f'branch {git.branch or ""}',
            f'tag {git.tag or ""}',
            f'hash {commit_hash}',
            f'archive {archive_name}',
        ]
        write_text_to(self.mama_shim_file(), '\n'.join(lines) + '\n')
        # Invalidate, do not set True: a real .git can also be present.
        self._is_shim_cache = None


    def read_shim_marker(self) -> dict:
        """Return a dict of shim metadata, or an empty dict when there is no marker.
        Keys: name, url, branch, tag, hash, archive."""
        return read_shim_marker_at(self.build_dir)


    def remove_shim_marker(self):
        path = self.mama_shim_file()
        if os.path.exists(path):
            os.remove(path)
        self._is_shim_cache = False


    def try_load_cached_shim(self, check_staleness: bool = True):
        """Use the local cache of an existing shim. Return the configured BuildTarget, or None on a miss or a stale shim.
        check_staleness: if True, run ls-remote first and drop the marker when upstream advanced"""
        from .artifactory import artifactory_load_target  # local import: avoid cycle
        from .build_target import BuildTarget
        from .types.git import Git

        if not self.is_artifactory_shim(): return None

        marker = self.read_shim_marker()
        stored_hash = marker.get('hash', '')
        if not stored_hash: return None

        # A pinned self.version replaces the commit hash in the archive name, so a shim cached under
        # another name predates the pin and is exactly the stale package it invalidates. Re-probe instead.
        pinned = pinned_version(self)
        stored_archive = marker.get('archive', '')
        if pinned and stored_archive and not stored_archive.endswith(f'-{pinned}'):
            if self.config.print:
                warning(f'  - Target {self.name: <16} SHIM STALE archive={stored_archive} '
                        f'!= pinned version {pinned}')
            self.remove_shim_marker()
            return None

        if check_staleness:
            git: Git = self.dep_source
            # ls-remote is a cheap remote-ref probe, not a package fetch - allowed under noart.
            current_hash = git.init_commit_hash(self, use_cache=False, fetch_remote=True)
            if current_hash and current_hash != stored_hash:
                if self.config.print:
                    warning(f'  - Target {self.name: <16} SHIM STALE was={stored_hash} now={current_hash}')
                self.remove_shim_marker()
                return None

        probe_target = BuildTarget(name=self.name, config=self.config, dep=self, args=self.target_args)
        fetched, dependencies = artifactory_load_target(probe_target, self.build_dir, num_files_copied=0)
        if not fetched: return None
        self.artifactory_archive = stored_archive
        if dependencies: self.add_children(dependencies)
        if self.config.print:
            console(f'  - Target {self.name: <16} SHIM CACHED {marker.get("archive", "")}', color=Color.GREEN)
        return probe_target


    def create_build_dir_if_needed(self):
        if not os.path.exists(self.build_dir): # check to avoid Access Denied errors
            os.makedirs(self.build_dir, exist_ok=True)


    ## @return True if the dependency has changed
    def load(self):
        # Under parallel_load a shared (diamond) dep can get two concurrent load() calls. The lock
        # serializes loads of THIS dep only, so exactly one thread clones it and other deps stay concurrent.
        with self._load_lock:
            if self.already_loaded:
                return self.should_rebuild
            return self._load()


    def _load_target(self) -> BuildTarget:
        self.create_build_target() ## parses target mamafile
        self._update_dep_name_and_dirs(self.name) ## requires target mamafile workspace
        self.create_build_dir_if_needed()
        return self.target


    def skim(self):
        """Name the children of this dep without loading it. The walk uses this to find a target.

        A skim parses the mamafile and runs settings() and dependencies(), because only those two
        hooks name a child. It fetches nothing, it clones nothing and it creates no build dir. The
        hooks run once, so the later load must not repeat them, and add_child cannot raise.

        The root never skims. Its load locks the compiler, and every dep dir name below reads that."""
        if self.is_root or self.did_skim or self.already_loaded: return
        self.did_skim = True
        self.create_build_target()
        self._update_dep_name_and_dirs(self.name)  # requires the workspace the mamafile names
        self.skimming = True  # a hook that reads a dep path now gets a raise, not a half-resolved path
        try:
            self.target.settings()
            self.target.dependencies()
        finally:
            self.skimming = False

    def _git_checkout_if_needed(self) -> bool:
        # A shim has no working tree. The ls-remote in try_load_artifactory_shim checks upstream.
        if self.is_artifactory_shim():
            return False
        if not self.is_root and self.dep_source.is_git:
            git:Git = self.dep_source
            return git.dependency_checkout(self)
        return False


    def _defer_load(self) -> bool:
        """True when a targeted run skips every network step of a dep outside the target: the shim
        probe, the package fetch and the clone. The dep keeps its name, so the graph still holds it.

        This is the first of the two load stages. Stage one explores the graph for free, and it reads
        only what the disk already answers. After the load, revive_deferred_target_deps runs stage two
        and loads the deferred deps that the subtree of the target needs."""
        config = self.config
        if not config.target or config.targets_all() or config.deps_only: return False
        if self.is_current_target() or self.clone_revived: return False
        if not self.dep_source.is_git: return False  # a local dep is already on disk, a pkg dep is one url
        # source on disk costs nothing to read, and it grows the graph, so it always loads
        if self.is_real_clone() or has_source_content(self.src_dir): return False
        self.load_deferred = True
        if config.verbose:
            console(f'  - Target {self.name: <16} LOAD deferred (outside target {config.target})', color=Color.BLUE)
        return True


    def load_is_free(self) -> bool:
        """True when this deferred dep loads with no network, because a cached package answers from disk.
        `update` and `noart` both re-probe the remote, so under either one no cached package is free."""
        return self.is_artifactory_shim() and not (self.config.update or self.config.disable_artifactory)


    def revive_deferred_load(self):
        """Forget a deferred load, so the next load() fetches or clones the dep and parses its mamafile.

        The children go too. A parent-supplied mamafile makes a deferred load name children of its own,
        and the real load runs dependencies() again. add_child refuses a child the list already holds,
        so keeping them crashes the run. Each one stays in the dep registry, so the second call
        re-attaches the same instance."""
        self.load_deferred = False
        self.clone_revived = True
        self.already_loaded = False
        self.children = []
        self.target = None # the deferred load parsed no mamafile, so self.target holds a default BuildTarget


    def _force_source_clone(self) -> bool:
        """This run must produce a real clone, even from a cached shim.

        `noart` forces it for EVERY dep, because it refuses the artifactory and source is what remains.
        A `rebuild`, `unshallow` or `wipe` forces it for the target it names. Only the git path honors a
        wipe, so a shim that loads here would skip it.
        A plain `clean` does NOT force a clone. It reloads the package after the clean."""
        conf = self.config
        if conf.disable_artifactory: return True
        return (conf.rebuild or conf.unshallow or conf.reclone) and self.is_current_target()


    def _drop_stale_shim_marker(self):
        """Remove a marker left where a real clone now exists: a rebuild, unshallow, or another platform's
        build can clone without dropping it. is_artifactory_shim() then returns False, so deploy runs,
        but papa_deploy refuses any directory with a marker."""
        if self.is_real_clone() and has_shim_marker(self.build_dir):
            if self.config.verbose: console(f'  - Target {self.name: <16} STALE shim marker dropped (real clone on disk)')
            self.remove_shim_marker()


    def _try_artifactory_shim(self) -> bool:
        """Pre-clone artifactory load for a non-root git dep: a cached shim or an ls-remote probe.
        Return True when the dep loads without a clone."""
        self._drop_stale_shim_marker()
        # rebuild/unshallow target: drop the shim marker so the git path clones source.
        if self._force_source_clone():
            if self.is_artifactory_shim():
                if self.config.print:
                    console(f'  - Target {self.name: <16} SHIM dropped, this run needs source', color=Color.BLUE)
                self.remove_shim_marker()
            # suppress the post-clone probe so it cannot reload the package over the clone (also for an already-cloned target)
            self.did_check_artifactory = True
            return False
        # Plain `mama build` trusts the cached shim. Under `update` the regular probe re-extracts, and
        # `noart` never arrives here, because it forces the source clone above.
        if self.is_artifactory_shim() and not self.config.update:
            cached = self.try_load_cached_shim(check_staleness=False)
            if cached is not None:
                self.target = cached
                self.did_check_artifactory = True
                return True
        # regular shim probe: for an already-cloned dep the update path (fetch+reset) is correct, so skip it
        if not self.is_real_clone() and self.can_fetch_artifactory(print=False, which='SHIM'):
            shim_target, shim_deps = try_load_artifactory_shim(self)
            if shim_target is not None:
                self.target = shim_target
                self.did_check_artifactory = True
                if shim_deps: self.add_children(shim_deps)
                return True
            # The probe found no package. An `update` still has to move the dep forward, so drop a
            # marker whose commit upstream has left behind and let the git path clone the source.
            if self.config.update: self._drop_shim_if_upstream_moved()
        return False


    def _drop_shim_if_upstream_moved(self) -> bool:
        """Remove a shim marker whose recorded commit no longer matches upstream. Return True when it
        went. A missing package for an UNCHANGED commit keeps the shim, because the files it already
        extracted are still the right ones. The probe resolved the current commit, so this costs nothing."""
        if not self.is_artifactory_shim(): return False
        stored = self.read_shim_marker().get('hash', '')
        current = self.dep_source.commit_hash
        if not (stored and current and current != stored): return False
        if self.config.print:
            warning(f'  - Target {self.name: <16} SHIM STALE was={stored} now={current}, building from source')
        self.remove_shim_marker()
        return True


    def _try_artifactory_load(self, target) -> bool:
        """Post-clone artifactory probe. It covers the target.version case:
        the archive name is unknown until the mamafile parse."""
        if not self.should_load_artifactory(): return False
        if self.can_fetch_artifactory(print=True, which='LOAD'):
            self.did_check_artifactory = True
            fetched, dependencies = artifactory_fetch_and_reconfigure(target)
            if fetched:
                self.add_children(dependencies)
                return True
            if self.dep_source.is_pkg:
                raise RuntimeError(f'  - Target {self.name} failed to load artifactory pkg {self.dep_source}')
        elif self.is_force_art_target():
            raise RuntimeError(f'  - Target {self.name} failed to find artifactory pkg {self.dep_source} but `art` was specified')
        return False


    def _reload_artifactory_after_clean(self, target) -> bool:
        """Re-extract the artifactory package that a plain `clean` removed. The cached zip lives in dep_dir,
        so this works offline. On failure the caller continues to the regular post-clone probe."""
        self.create_build_dir_if_needed()
        fetched, dependencies = artifactory_fetch_and_reconfigure(target)
        if fetched and dependencies: self.add_children(dependencies)
        return bool(fetched)


    def _load(self):
        conf = self.config
        if conf.verbose:
            console(f'  - Target {self.name: <16} LOAD ({self.dep_source.get_type_string()})', color=Color.BLUE)

        is_target = self.is_current_target()
        loaded_from_pkg = False
        git_changed = False

        if self.is_root:
            # A root target loads its BuildTarget at once: the workspace comes from its mamafile.
            target = self._load_target()
        else:
            # A non-root target only creates the required dirs. The mamafile loads after the shim or clone step.
            self._update_dep_name_and_dirs(self.name)
            self.create_build_dir_if_needed()
            if not self._defer_load():
                if self.dep_source.is_git:
                    # One dep_dir-keyed cross-process lock over BOTH the shim setup and the checkout: a sibling
                    # `mama <host> build` process must never read a half-written clone as a broken tree.
                    # Different deps never contend, and the mamafile parse below needs no lock.
                    with interprocess_dir_lock(self.dep_dir, timeout=_LOAD_LOCK_TIMEOUT_SEC):
                        loaded_from_pkg = self._try_artifactory_shim()
                        # a clean never needs source: without this guard a shim-less dep clones minutes of git, then deletes it
                        if not loaded_from_pkg and not conf.clean_only():
                            git_changed = self._git_checkout_if_needed() ## pull git before the target mamafile load
                elif not conf.clean_only():
                    git_changed = self._git_checkout_if_needed()  # non-git local source: no shared tree to lock
            target = self._load_target() ## load target for Git and Src

        if conf.clean and is_target:
            self.clean() ## requires a parsed mamafile target
            # a plain clean removed the shim-loaded package libs: re-extract so dependents can link (rebuild dropped the shim)
            if loaded_from_pkg:
                loaded_from_pkg = self._reload_artifactory_after_clean(target)

        # a deferred dep skips this probe too: the archive name needs a commit hash, which costs an ls-remote
        if not self.is_root and not loaded_from_pkg and not self.load_deferred:
            # The post-clone probe covers target.version-pinned deps that the pre-clone shim could not predict.
            loaded_from_pkg = self._try_artifactory_load(target)
            if not loaded_from_pkg:
                self.load_build_products(target)

        if conf.verbose:
            console(f'  - Target {self.name: <16} load settings and dependencies')
        # A skim already ran both hooks and kept what they named. Running them twice would append a
        # setting twice, and add_child would refuse the child it already holds.
        if not self.did_skim:
            target.settings() ## customization point for project settings
            if self.is_root:
                conf.lock_compiler()  # root settings() is the last prefer_clang/gcc call, lock before any dep loads
                conf.init_platform_toolchain()  # after settings(), so its set_*_toolchain() beats the default probe
                self._update_dep_name_and_dirs(self.name)  # the build_dir predates the compiler lock, so re-resolve it
            target.dependencies() ## customization point for additional dependencies

        if not loaded_from_pkg and self.is_root:
            conf.get_preferred_compiler_paths() # fetch the compiler immediately from root settings

        build = False
        if conf.build or conf.update:
            build = self._should_build(conf, target, is_target, git_changed, loaded_from_pkg)
            if build: self.create_build_dir_if_needed() # in case we just cleaned
            if git_changed:
                git:Git = self.dep_source
                git.save_status(self)

        self.load_action = self._display_load_action(loaded_from_pkg)  # refine the breakdown letter (G/L/A)
        self.already_loaded = True
        self.should_rebuild = build
        if conf.list: self._print_list(conf, target)
        return build


    def _display_load_action(self, loaded_from_pkg: bool) -> str:
        """The load label for the display breakdown letter: artifactory (A) / local (L), else the
        git action (check/clone/pulling -> G) that the checkout recorded."""
        if loaded_from_pkg:        return 'artifactory'
        if self.dep_source.is_src: return 'local'
        return self.load_action


    def can_fetch_artifactory(self, print: bool, which: str):
        if self.is_root or self.did_check_artifactory:
            return False
        # An `add_artifactory_pkg` dep exists ONLY as a package. No flag can make it build from source,
        # so `noart`, a rebuild and a clean must never refuse its fetch.
        if self.dep_source.is_pkg: return True

        force_art = self.config.force_artifactory
        disable_art = self.config.disable_artifactory
        is_target = self.is_current_target()

        def noart(r, expected=False):
            # `expected`: a clean or rebuild skips artifactory by design, so show the line only under verbose
            show = self.config.verbose if expected else (self.config.print or force_art)
            if print and show:
                warning(f'  - Target {self.name: <16} NO ARTIFACTORY PKG [{which} {r}]')
            self.did_check_artifactory = True
            return False

        if disable_art:
            return noart('noart override')
        elif is_target and not force_art:
            # do not load during a rebuild, defer to the source build
            if self.config.rebuild: return noart('target rebuild', expected=True)
            # do not load during a clean, the clean deletes it anyway
            if self.config.clean: return noart('target clean', expected=True)
        elif print and (self.config.verbose or force_art):
            warning(f'  - Target {self.name: <16} CHECK ARTIFACTORY PKG [{which}]')

        return True


    def is_force_art_target(self):
        return not self.is_root and self.config.force_artifactory and self.is_current_target()


    def should_load_artifactory(self):
        if self.is_root or self.did_check_artifactory:
            return False
        should_load = self.dep_source.is_pkg \
            or os.path.exists(self.papa_package_file()) \
            or self.is_first_time_build()
        is_force_art_target = self.is_force_art_target()
        return should_load or is_force_art_target


    def _print_list(self, conf, target):
        if conf.print:
            console(f'  - Target {target.name: <16}')


    def _should_build(self, conf:BuildConfig, target:BuildTarget, is_target, git_changed, loaded_from_pkg):
        def build(r):
            if conf.print:
                args = f'  {target.args}' if target.args else ''
                warning(f'  - Target {target.name: <16} BUILD [{r}]{args}')
            return True

        # An artifactory shim has no source on disk, so there is nothing to build. A rebuild requires
        # `mama unshallow` to convert it to a real clone first.
        if self.is_artifactory_shim():
            return False

        if conf.target and not is_target: # the run named another target
            return False # skip: mark_unbuilt_target_deps() re-marks the deps the target needs after the load

        ## a build also packages
        if conf.clean and is_target: return build('cleaned target')
        if conf.run_cmake_configure and is_target: return build('cmake reconfigure')
        if self.is_root:             return build('root target')
        if self.always_build:        return build('always build')
        if git_changed:              return build('git commit changed')
        if self.dep_source.is_pkg:   return build('artifactory pkg')

        # in-place source edits of a git dep: fast working-tree fingerprint, no reconfigure
        if self.dep_source.is_git and self.is_real_clone():
            if self.dep_source.source_tree_changed(self): return build('source modified')

        # in-place edits of a local dep: the same fast fingerprint path, so a root build does not skip a modified subfolder
        if self.dep_source.is_src and self.dep_source.source_tree_changed(self):
            return build('source modified')

        if conf.update and conf.target == target.name:
            return build('update target='+conf.target)

        if not self.is_root and conf.build and conf.target == target.name:
            return build('build target='+conf.target)

        # a build product that a previous build or download recorded but is now missing forces a rebuild
        missing_product = self.find_first_missing_build_product()
        if missing_product:
            return build(f'{missing_product} does not exist')

        # `nothing_to_build` marks a header-only project, so check if the build should run
        can_build = not loaded_from_pkg and not self.nothing_to_build
        if can_build:
            # no build products defined: the project never built and never downloaded
            if not target.build_products:
                if not self.has_build_files():
                    return build('not built yet')
                return build('no build dependencies')

            # build products exist and none are missing, so nothing to do
            if target.build_products and not missing_product:
                pass

        # a removed dependency changes the link list, so rebuild
        missing_dep = self.find_missing_dependency()
        if missing_dep: return build(f'{missing_dep} was removed')

        if not self.from_artifactory:
            if self.update_mamafile_tag(): return build(f'{short_path(self.mamafile_path())} modified')
            if self.update_cmakelists_tag(): return build(f'{short_path(self.cmakelists_path())} modified')

        if conf.print:
            console(f'  - Target {target.name: <16} OK', color=Color.GREEN)
        return False # do not build, all is ok


    def after_load(self):
        # A shim has no source, so a changed child cannot change what it produces. Without this guard the
        # rebuild flag makes _run_packaging re-derive the exports and lose the papa.txt data of the real build.
        if self.config.no_specific_target() and not self.is_artifactory_shim():
            first_changed = next((c for c in self.children if c.should_rebuild), None)
            if first_changed and not self.should_rebuild:
                self.should_rebuild = True
                if self.config.print:
                    console(f'  - Target {self.name: <16} BUILD [{first_changed.name} changed]')
                self.create_build_dir_if_needed() # in case we just cleaned


    def successful_build(self):
        self.update_mamafile_tag()
        self.update_cmakelists_tag()
        self.save_dependency_list()
        if self.dep_source.is_git:
            git:Git = self.dep_source
            git.save_status(self)
        elif self.dep_source.is_src:
            self.dep_source.save_status(self)


    def create_build_target(self):
        if self.target:
            self.target._set_args(self.target_args)
            return

        from .build_target import BuildTarget as mamaBuildTarget  # deferred: circular at import time
        mamaFilePath = self.mamafile_path()
        if mamaFilePath and self.config.verbose:
            exists = os.path.exists(mamaFilePath)
            relpath = os.path.relpath(mamaFilePath)
            console(f'  - Target {self.name: <16} Load Mamafile: {relpath} (Exists={exists})', color=Color.BLUE)

        # parse the project-specific BuildTarget subclass from the mamafile
        project, buildTarget = parse_mamafile(self.config, mamaBuildTarget, mamaFilePath)
        if project and buildTarget:
            buildStatics = buildTarget.__dict__
            if not self.workspace:
                if   'workspace'        in buildStatics: self.workspace = buildStatics['workspace']
                elif 'local_workspace'  in buildStatics: self.workspace = buildStatics['local_workspace']
                elif 'global_workspace' in buildStatics: self.workspace = buildStatics['global_workspace']
                else:                                    self.workspace = 'packages'
            if self.is_root:
                if   'workspace'        in buildStatics: self.config.global_workspace = False
                elif 'local_workspace'  in buildStatics: self.config.global_workspace = False
                elif 'global_workspace' in buildStatics: self.config.global_workspace = True
                if not self.config.global_workspace:
                    self.config.workspaces_root = self.src_dir
            self.target = buildTarget(name=project, config=self.config, dep=self, args=self.target_args)
        else:
            if not self.workspace:
                self.workspace = 'packages'
            # A root with no mamafile still owns its workspace. Without this the field keeps its HOME
            # default, and every dep dir of a CMakeLists-only project lands in the home dir.
            if self.is_root and not self.config.global_workspace:
                self.config.workspaces_root = self.src_dir
            if self.config.verbose:
                warning(f'  - Target {self.name: <16} Using Default BuildTarget Project={project} BuildTarget={buildTarget}')
            self.target = mamaBuildTarget(name=self.name, config=self.config, dep=self, args=self.target_args)


    def is_current_target(self):
        return self.config.target_matches(self.name)


    def is_root_or_config_target(self):
        return self.is_root or self.is_current_target()


    def cmakelists_path(self):
        cmake_lists_path = self.target.cmake_lists_path
        if cmake_lists_path.startswith('/'):
            return cmake_lists_path # absolute path
        return normalized_join(self.src_dir, cmake_lists_path)


    def cmakelists_exists(self):
        return os.path.exists(self.cmakelists_path())


    def cmake_source_dir(self):
        """The dir cmake configures, which holds the CMakeLists.txt of this dep. A bare
        `include(mama.cmake)` resolves against this dir, so the proxy belongs in it."""
        return os.path.dirname(self.cmakelists_path()) or self.src_dir


    def default_mama_cmake_path(self):
        return normalized_join(self.cmake_source_dir(), MAMA_CMAKE)


    def mama_cmake_paths(self) -> list:
        """Every path a proxy `include()` names, resolved against the dir of the file that names it. The
        scan never caches: a configure() hook can rewrite the CMakeLists.txt with no change a stat sees."""
        cmake_dir = self.cmake_source_dir()
        # realpath, because a symlink inside the source dir leads out of it, and a plain prefix test misses that
        roots = tuple(os.path.realpath(d) + os.sep for d in (self.src_dir, cmake_dir))
        paths = []
        for source_dir, project_dir, arg in find_mama_cmake_includes(self.cmakelists_path(), cmake_dir):
            # a variable mama does not expand, such as $ENV{}, means the default answers
            unknown = has_unknown_cmake_var(arg)
            path = normalized_join(source_dir, MAMA_CMAKE if unknown else
                                   expand_cmake_dirs(arg, source_dir, project_dir, cmake_dir))
            if not os.path.realpath(path).startswith(roots):
                warning(f'{self.name}: mama writes no proxy outside its source dir: include({arg})')
            elif path not in paths:
                paths.append(path)
        return paths


    def ensure_cmakelists_exists(self):
        if not os.path.exists(self.cmakelists_path()):
            raise IOError(f'{self.cmakelists_path()} not found. Add a CMakeLists.txt (the file name is case' + \
                          ' sensitive), or call `self.nothing_to_build()` in the configure step.')


    def mamafile_path(self):
        if self.mamafile: return self.mamafile
        if self.src_dir: return normalized_join(self.src_dir, 'mamafile.py')
        return None


    def mamafile_exists(self):
        return os.path.exists(self.mamafile_path())


    def update_mamafile_tag(self):
        # a shim has no source to tag: the short-circuit keeps a parent-mamafile fetch from flagging it modified every run
        if self.is_artifactory_shim():
            return False
        return self.src_dir and update_mamafile_tag(self.config, self.mamafile_path(), self.build_dir)


    def update_cmakelists_tag(self):
        if self.is_artifactory_shim():
            return False
        return self.src_dir and update_cmakelists_tag(self.config, self.cmakelists_path(), self.build_dir)


    def build_file_exists(self, filename):
        """ True if the file exists relative to build_dir. """
        return os.path.exists(normalized_join(self.build_dir, filename))


    def sanitizer_list_path(self):
        return normalized_join(self.build_dir, 'enabled_sanitizers')


    def get_enabled_sanitizers(self):
        list_path = self.sanitizer_list_path()
        if os.path.exists(list_path):
            return read_text_from(list_path)
        return ''


    def save_enabled_sanitizers(self):
        sanitizers_file = self.sanitizer_list_path()
        if self.target.config.sanitize:
            write_text_to(sanitizers_file, self.target.config.sanitize)
        elif os.path.exists(sanitizers_file): # delete the file to record that no sanitizer was in use
            os.remove(sanitizers_file)


    def coverage_enabled_path(self):
        return normalized_join(self.build_dir, 'enabled_coverage')


    def get_enabled_coverage(self):
        return os.path.exists(self.coverage_enabled_path())


    def save_enabled_coverage(self):
        coverage_file = self.coverage_enabled_path()
        if self.target.config.coverage:
            write_text_to(coverage_file, self.target.config.coverage)
        elif os.path.exists(coverage_file):
            os.remove(coverage_file)
    

    def path_relative_to_us(self, relpath) -> str:
        """Convert a relative path to absolute. The base is this mamafile dir, else the source dir."""
        if not relpath or os.path.isabs(relpath):
            return relpath # the path is already None or absolute
        elif self.mamafile:
            return normalized_join(os.path.dirname(self.mamafile), relpath)
        else:
            if not self.src_dir: # an artifactory pkg has no source dir
                return relpath
            return normalized_join(self.src_dir, relpath)


    def get_mamafile_path_relative_to_us(self, name, relative_mamafile) -> str:
        """Resolve a relative mamafile path against this dep, else look for mama/<name>.py.
        Return None when neither exists."""
        if relative_mamafile:
            local_mamafile = self.path_relative_to_us(relative_mamafile)
            if not os.path.exists(local_mamafile):
                raise OSError(f'mama add {name} failed! local mamafile does not exist: {local_mamafile}')
            return local_mamafile
        maybe_mamafile = self.path_relative_to_us(f'mama/{name}.py')
        if os.path.exists(maybe_mamafile):
            return maybe_mamafile
        return None


    # "name(-branch)"
    def get_dependency_name(self):
        if self.dep_source.is_git:
            git:Git = self.dep_source
            branch = git.branch_or_tag()
            if branch:
                return self.name + '-' + branch
        return self.name


    def save_dependency_list(self):
        deps = [dep.get_dependency_name() for dep in self.get_children()]
        write_text_to(f'{self.build_dir}/mama_dependency_libs', '\n'.join(deps))


    def find_missing_dependency(self):
        last_build = [dep.rstrip() for dep in read_lines_from(f'{self.build_dir}/mama_dependency_libs')]
        current = [dep.get_dependency_name() for dep in self.get_children()]
        for last in last_build:
            if not (last in current):
                return last.strip()
        return None # Nothing missing


    def clean(self):
        if self.config.print:
            console(f'  - Target {self.name: <16} CLEAN  {self.build_dir_name}')

        if self.build_dir == '/' or not os.path.exists(self.build_dir):
            return

        self.target.clean() # Customization point
        shutil.rmtree(self.build_dir, ignore_errors=True)


    def dirty(self):
        """ Force the next build: remove a build product, the mamafile_tag, papa.txt and the shim marker. """
        if self.config.print: console(f'  - Target {self.name: <16} Dirty')

        if self.target.build_products:
            # make sure no valid build product remains to link against
            depfile = self.target.build_products[0]
            if os.path.exists(depfile):
                os.remove(depfile)
                if self.config.verbose: console(f'    dirty: removed {depfile}')

        if self.build_dir_exists():
            # the mamafile_tag detects a mamafile.py change
            mamafile_tag = normalized_join(self.build_dir, 'mamafile_tag')
            if os.path.exists(mamafile_tag):
                os.remove(mamafile_tag)
                if self.config.verbose: console('    dirty: removed mamafile_tag')

            # artifactory packages need this
            papafile = self.papa_package_file()
            if os.path.exists(papafile):
                os.remove(papafile)
                if self.config.verbose: console('    dirty: removed papa.txt')

            # remove the shim marker so the next build re-checks artifactory freshness
            self.remove_shim_marker()
