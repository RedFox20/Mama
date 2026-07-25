from __future__ import annotations
from typing import List, TYPE_CHECKING
import os, sys, shutil, time, threading

from .types.dep_source import DepSource
from .types.git import Git
from .types.local_source import LocalSource
from .utils.system import Color, console, error, warning
from .utils.dir_lock import interprocess_dir_lock
from .artifactory import artifactory_fetch_and_reconfigure, try_load_artifactory_shim, resolve_pinned_version
from .util import normalized_join, normalized_path, read_text_from, write_text_to, read_lines_from, MAMA_SHIM_FILENAME
from .parse_mamafile import parse_mamafile, update_mamafile_tag, update_cmakelists_tag
import mama.package as package


if TYPE_CHECKING:
    from .build_config import BuildConfig
    from .build_target import BuildTarget


# Backstop for the cross-process dep-dir lock: a single dep's shim/clone is seconds-to-minutes, and mama's
# git_timeout kills a stalled clone (releasing the lock), so a waiter almost never approaches this. If it
# does, it proceeds unlocked rather than hang - a genuinely stuck holder can't block a build forever.
_LOAD_LOCK_TIMEOUT_SEC = 300


######################################################################################


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
        self.load_action = 'check'  # what load() did, for the display: check|clone|pulling|local|artifactory
        self.phase_times = {}  # 'load'|'configure'|'build' -> wall seconds, for the `buildstats` breakdown
        self._load_lock = threading.Lock()  # serialises concurrent load() of THIS dep (parallel_load)
        self.from_artifactory = False # if true, this Dependency was loaded from Artifactory
        self.did_check_artifactory = False # if true, artifactory was already checked and can be skipped
        self._is_shim_cache = None # tri-state cache for is_artifactory_shim()
        self.is_root = parent is None # Root deps are always built
        self.children: List[BuildDependency] = []
        self.product_sources = []
        self.flattened_deps: List[BuildDependency] = [] # flat dependencies only, nothing else

        self.src_dir = None # source directory where the code is located
        self.dep_dir = None # dependency dir where platform build dirs are kept
        self.build_dir = None # {dep_dir}/{config.platform_build_dir_name()}
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
            # put the git repo in workspace
            self.src_dir = normalized_join(self.dep_dir, self.name)
        elif dep_source.is_pkg:
            if not config.artifactory_ftp:
                raise RuntimeError(f'add_artifactory_pkg({self.name}) failed because config.artifactory_ftp is not set!')
            self.src_dir = None # there is no src_dir when using artifactory packages
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
        if args: # only add non-empty args (bugfix)
            for arg in args:
                if arg:
                    self.target_args.append(arg)


    def update_existing_dependency(self, dep_source: DepSource):
        if dep_source.is_git or dep_source.is_src:
            self._add_args(dep_source.args)
            if self.target:
                self.target._set_args(self.target_args)


    def add_child(self, dep_source: DepSource) -> BuildDependency:
        """
        Adds a new child dependency to this BuildDependency. Thread-safe: under parallel_load two
        parents can add the same-named child concurrently; the registry lock makes the dedup +
        creation atomic so a shared (diamond) dep resolves to one instance.
        """
        with self.config.dep_registry_lock:
            dep = self.config.loaded_dependencies.get(dep_source.name)
            if dep:
                # reuse & update existing dep
                dep.update_existing_dependency(dep_source)
            else:
                # add new
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
        """Adds papa.txt-declared children, skipping any already present. One dep can load its
        artifactory package twice - shim probe, then the post-clean re-extract - and both report the
        same list; add_child's duplicate raise must stay for genuine mamafile double-declares."""
        existing = {c.name for c in self.children}
        for dep_source in dep_sources:
            if dep_source.name not in existing: self.add_child(dep_source)


    def get_children(self) -> List[BuildDependency]:
        """ Gets already resolved dependencies """
        if self.children is None:
            raise RuntimeError(f'Target {self.name} child dependencies unresolved')
        return self.children


    def _update_dep_name_and_dirs(self, name):
        self.name = name
        dep_name = name
        # TODO: using branch or tag in the dep name complicates the whole package system
        #       while only adding marginal value.
        # if self.dep_source.is_git:
        #     git:Git = self.dep_source
        #     if git.branch:
        #         branch_name = git.branch.replace('/', '-') # BUGFIX: branches with slashes
        #         dep_name = f'{self.name}-{branch_name}'
        #     elif git.tag:
        #         dep_name = f'{self.name}-{git.tag}'
        self.dep_dir = normalized_join(self.config.workspaces_root, self.workspace, dep_name)
        self.build_dir = normalized_join(self.dep_dir, self.config.platform_build_dir_name())


    def has_build_files(self):
        return self.build_file_exists('CMakeCache.txt') \
            or self.build_file_exists('Makefile')


    def is_first_time_build(self):
        # conditions for considering this as a first-time build
        # - rebuild: always first time build
        # - no_build_files: definitely a first time build
        def first_time_build():
            return not self.build_file_exists('mamafile_tag') \
                and not self.build_file_exists('CMakeCache.txt')
        return self.config.rebuild or first_time_build()


    def exported_libs_file(self):
        return self.build_dir + '/mama_exported_libs'


    def papa_package_file(self):
        return self.build_dir + '/papa.txt'


    def load_build_products(self, target):
        """ These are the build products that were generated during last build """
        loaded_deps = read_lines_from(self.exported_libs_file())
        if loaded_deps:
            package.set_export_libs_and_products(target, loaded_deps)


    def save_exports_as_dependencies(self, exports):
        write_text_to(self.exported_libs_file(), '\n'.join(exports))


    def has_usable_artifacts(self) -> bool:
        """Is there anything on disk a dependent could link/include against? build_products carries the
        last build's recorded exports, so a custom build() target with no CMakeCache still reads as built."""
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
        """ Marker file path identifying this dep as an artifactory shim. """
        return normalized_join(self.build_dir, MAMA_SHIM_FILENAME)


    def is_artifactory_shim(self) -> bool:
        """True if this dep was loaded from artifactory without a git clone.
        Cached: state only changes via write/remove_shim_marker and dirty()."""
        if self._is_shim_cache is None:
            self._is_shim_cache = (self.dep_source.is_git and os.path.exists(self.mama_shim_file())
                                   and not self.is_real_clone())
        return self._is_shim_cache


    def is_real_clone(self) -> bool:
        """ True if this dep has an actual git working tree on disk. """
        return self.src_dir is not None and os.path.exists(f'{self.src_dir}/.git')


    def write_shim_marker(self, archive_name: str, commit_hash: str):
        """
        Persist shim metadata so subsequent runs (and Phase 7 transitions)
        can identify the shim and know which archive backed it.
        """
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
        # Invalidate (not set True): a real .git may also be present.
        self._is_shim_cache = None


    def read_shim_marker(self) -> dict:
        """
        Returns a dict of shim metadata, or empty dict if no marker.
        Keys: name, url, branch, tag, hash, archive.
        """
        result = {}
        path = self.mama_shim_file()
        if not os.path.exists(path):
            return result
        for line in read_lines_from(path):
            line = line.rstrip()
            if not line or line == 'shim 1':
                continue
            key, _, value = line.partition(' ')
            result[key] = value
        return result


    def remove_shim_marker(self):
        path = self.mama_shim_file()
        if os.path.exists(path):
            os.remove(path)
        self._is_shim_cache = False


    def try_load_cached_shim(self, check_staleness: bool = True):
        """Honour an existing shim's local cache. With `check_staleness`, ls-remote
        first and drop the marker on upstream advance. Returns the configured
        BuildTarget, or None on cache miss/staleness."""
        from .artifactory import artifactory_load_target  # local import: avoid cycle
        from .build_target import BuildTarget
        from .types.git import Git

        if not self.is_artifactory_shim(): return None

        marker = self.read_shim_marker()
        stored_hash = marker.get('hash', '')
        if not stored_hash: return None

        # A locally-pinned self.version renames the archive (it replaces the commit hash), so
        # a shim cached under a non-matching name predates the pin: a stale package the pin
        # was bumped precisely to invalidate. Re-probe instead of trusting it.
        pinned = resolve_pinned_version(self)
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
        if dependencies: self.add_children(dependencies)
        if self.config.print:
            console(f'  - Target {self.name: <16} SHIM CACHED {marker.get("archive", "")}', color=Color.GREEN)
        return probe_target


    def create_build_dir_if_needed(self):
        if not os.path.exists(self.build_dir): # check to avoid Access Denied errors
            os.makedirs(self.build_dir, exist_ok=True)


    ## @return True if dependency has changed
    def load(self):
        # Per-dep lock: under parallel_load a shared (diamond) dep can get two concurrent load()
        # calls; serialise loads of THIS dep so exactly one thread clones it (the old
        # `currently_loading` busy-wait had a TOCTOU race). Different deps still clone concurrently.
        with self._load_lock:
            if self.already_loaded:
                return self.should_rebuild
            return self._load()


    def _load_target(self) -> BuildTarget:
        self.create_build_target() ## parses target mamafile
        self._update_dep_name_and_dirs(self.name) ## requires target mamafile workspace
        self.create_build_dir_if_needed()
        return self.target

    def _git_checkout_if_needed(self) -> bool:
        # Shims have no working tree; upstream check happens via ls-remote in try_load_artifactory_shim.
        if self.is_artifactory_shim():
            return False
        if not self.is_root and self.dep_source.is_git:
            git:Git = self.dep_source
            return git.dependency_checkout(self)
        return False


    def _force_source_clone(self) -> bool:
        """A `rebuild` (build from source) or `mama unshallow` of THIS target must materialize a real
        clone, even from a cached shim: drop the shim so the git path clones source instead of reusing
        the prebuilt package. A plain `clean` does NOT force a clone - it reloads the package post-clean."""
        return (self.config.rebuild or self.config.unshallow) and self.is_current_target()


    def _try_artifactory_shim(self) -> bool:
        """Pre-clone artifactory load for non-root git deps. Either honours a
        cached shim or probes artifactory via ls-remote. Returns True when the
        dep was satisfied without a clone."""
        # rebuild/unshallow target: drop the shim marker so the git path clones source.
        if self._force_source_clone():
            if self.is_artifactory_shim():
                if self.config.print:
                    console(f'  - Target {self.name: <16} REBUILD shim -> source clone', color=Color.BLUE)
                self.remove_shim_marker()
            # Build-from-source means "use this target's source": suppress the post-clone probe so it
            # doesn't re-load the prebuilt pkg over the clone (true for an already-cloned target too).
            self.did_check_artifactory = True
            return False
        # Existing shim: trust the local cache under plain `mama build`. Under
        # noart, still ls-remote to catch upstream-advanced shims (a mismatch
        # drops the marker so the caller's git path takes over). Under `update`
        # the cached path is skipped entirely so the regular probe re-extracts.
        if self.is_artifactory_shim() and not self.config.update:
            cached = self.try_load_cached_shim(check_staleness=self.config.disable_artifactory)
            if cached is not None:
                self.target = cached
                self.did_check_artifactory = True
                return True
        # Regular shim probe: skip when a real clone already exists - for an
        # already-cloned dep the regular update path (fetch+reset) is correct.
        if not self.is_real_clone() and self.can_fetch_artifactory(print=False, which='SHIM'):
            shim_target, shim_deps = try_load_artifactory_shim(self)
            if shim_target is not None:
                self.target = shim_target
                self.did_check_artifactory = True
                if shim_deps: self.add_children(shim_deps)
                return True
        return False


    def _try_artifactory_load(self, target) -> bool:
        """Post-clone artifactory probe. Catches the target.version case where
        the archive name isn't predictable until the mamafile has been parsed."""
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
        """Re-fetch the artifactory package a plain `clean` just wiped from the build dir. The cached
        zip lives in dep_dir (clean only rmtree's build_dir), so this re-extracts offline. Returns True
        on success; on failure the caller falls through to the regular post-clone probe."""
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
            # For root targets, always load the BuildTarget immediately - we need the workspace from its mamafile.
            target = self._load_target()
        else:
            # For non-root targets, only create the required dirs; mamafile is loaded after the shim/clone step.
            self._update_dep_name_and_dirs(self.name)
            self.create_build_dir_if_needed()
            if self.dep_source.is_git:
                # One cross-process lock over BOTH the shim setup and the checkout: a sibling `mama <host>
                # build` (build_host_binary's bootstrap) can be materialising this SAME dep_dir, and a
                # checkout's reclone-wipe rmtree's the ENTIRE dep_dir - so it must never run while another
                # process shims or clones into it. Keyed on dep_dir; different deps never contend, so parallel
                # loads stay fully concurrent. Once the checkout returns the tree is a real clone, so the
                # mamefile parse below is safe unlocked (no other process will wipe a healthy clone).
                with interprocess_dir_lock(self.dep_dir, timeout=_LOAD_LOCK_TIMEOUT_SEC):
                    loaded_from_pkg = self._try_artifactory_shim()
                    # A clean deletes build dirs; it never needs source. Without this a dep whose shim marker a
                    # PREVIOUS clean removed gets cloned from scratch - minutes of git for a dir we then delete.
                    if not loaded_from_pkg and not conf.clean_only():
                        git_changed = self._git_checkout_if_needed() ## pull Git before loading target Mamafile
            elif not conf.clean_only():
                git_changed = self._git_checkout_if_needed()  # non-git local source: no shared tree to lock
            target = self._load_target() ## load target for Git and Src

        if conf.clean and is_target:
            self.clean() ## requires a parsed mamafile target
            # A plain `clean` rmtree'd the build dir, including a shim-loaded artifactory package's libs;
            # re-extract it so dependents can still link. (`rebuild` dropped the shim above -> from source.)
            if loaded_from_pkg:
                loaded_from_pkg = self._reload_artifactory_after_clean(target)

        if not self.is_root and not loaded_from_pkg:
            # Post-clone probe catches target.version-pinned deps that the pre-clone shim couldn't predict.
            loaded_from_pkg = self._try_artifactory_load(target)
            if not loaded_from_pkg:
                self.load_build_products(target)

        if conf.verbose:
            console(f'  - Target {self.name: <16} load settings and dependencies')
        target.settings() ## customization point for project settings
        if self.is_root:
            conf.lock_compiler()  # root settings() is the last prefer_clang/gcc; lock before any dep loads
            self._update_dep_name_and_dirs(self.name)  # build_dir was computed pre-flip, re-resolve it
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
        git action (check/clone/pulling -> G) already recorded during checkout."""
        if loaded_from_pkg:        return 'artifactory'
        if self.dep_source.is_src: return 'local'
        return self.load_action


    def can_fetch_artifactory(self, print: bool, which: str):
        if self.is_root or self.did_check_artifactory:
            return False

        force_art = self.config.force_artifactory
        disable_art = self.config.disable_artifactory
        is_target = self.is_current_target()

        def noart(r, expected=False):
            # `expected`: OUR decision to skip (a clean/rebuild ignores artifactory by design), so it's
            # verbose-only - a second line per dep next to its CLEAN/BUILD line is pure noise.
            show = self.config.verbose if expected else (self.config.print or force_art)
            if print and show:
                warning(f'  - Target {self.name: <16} NO ARTIFACTORY PKG [{which} {r}]')
            self.did_check_artifactory = True
            return False

        if disable_art:
            return noart('noart override')
        elif is_target and not force_art:
            # don't load during rebuild -- defer to source based builds in that case
            if self.config.rebuild: return noart('target rebuild', expected=True)
            # don't load anything during cleaning -- because it will get cleaned anyways
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

        # Artifactory shim: no source on disk, nothing to build from. The shim was
        # already (re-)loaded during _load(); a rebuild requires `mama unshallow`
        # to convert it to a real clone first.
        if self.is_artifactory_shim():
            return False

        if conf.target and not is_target: # if we called: "target=SpecificProject"
            return False # skip; mark_unbuilt_target_deps() revives the ones X actually needs, post-load

        ## build also entails packaging
        if conf.clean and is_target: return build('cleaned target')
        if conf.run_cmake_configure and is_target: return build('cmake reconfigure')
        if self.is_root:             return build('root target')
        if self.always_build:        return build('always build')
        if git_changed:              return build('git commit changed')
        if self.dep_source.is_pkg:   return build('artifactory pkg')

        # in-place source edits of a git dep: fast working-tree fingerprint, no reconfigure
        if self.dep_source.is_git and self.is_real_clone():
            if self.dep_source.source_tree_changed(self): return build('source modified')

        # in-place edits of a local dep tracked by an enclosing git repo: same fast fingerprint
        # path, so a large root build doesn't silently skip a modified subfolder.
        if self.dep_source.is_src and self.dep_source.source_tree_changed(self):
            return build('source modified')

        # if we call `update this_target`
        if conf.update and conf.target == target.name:
            return build('update target='+conf.target)

        # if we call sub-dependency `build this_target`
        if not self.is_root and conf.build and conf.target == target.name:
            return build('build target='+conf.target)

        # if the project has been built at least once or downloaded from artifactory package
        # then there will be a list of build products
        # if any of those are missing, then this needs to be rebuilt to re-acquire them
        missing_product = self.find_first_missing_build_product()
        if missing_product:
            return build(f'{missing_product} does not exist')

        # project has not defined `nothing_to_build` which is for header-only projects
        # thus we need to check if build should execute
        can_build = not loaded_from_pkg and not self.nothing_to_build
        if can_build:
            # there are no build products defined at all, it hasn't been built or downloaded
            if not target.build_products:
                if not self.has_build_files():
                    return build('not built yet')
                return build('no build dependencies')

            # we have build products, and none of them are missing
            if target.build_products and not missing_product:
                pass # added this condition for clarity -- all should be OK

        # something changed in the mamafile, or artifactory package
        # and the list of dependency targets changed, thus we need to rebuild
        missing_dep = self.find_missing_dependency()
        if missing_dep: return build(f'{missing_dep} was removed')

        if not self.from_artifactory:
            if self.update_mamafile_tag(): return build(target.name+'/mamafile.py modified')
            if self.update_cmakelists_tag(): return build(target.name+'/CMakeLists.txt modified')

        if conf.print:
            console(f'  - Target {target.name: <16} OK', color=Color.GREEN)
        return False # do not build, all is ok


    def after_load(self):
        if self.config.no_specific_target():
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

        # load the default mama.BuildTarget class
        from .build_target import BuildTarget as mamaBuildTarget  # deferred: circular at import time
        mamaFilePath = self.mamafile_path()
        if mamaFilePath and self.config.verbose:
            exists = os.path.exists(mamaFilePath)
            relpath = os.path.relpath(mamaFilePath)
            console(f'  - Target {self.name: <16} Load Mamafile: {relpath} (Exists={exists})', color=Color.BLUE)

        # this will load the specific `<class project(mama.build_target)>` class
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


    def ensure_cmakelists_exists(self):
        if not os.path.exists(self.cmakelists_path()):
            raise IOError(f'Could not find {self.cmakelists_path()}! Add a CMakelists.txt, or add `self.nothing_to_build()` to configuration step. Also note that filename CMakeLists.txt is case sensitive.')


    def mamafile_path(self):
        if self.mamafile: return self.mamafile
        if self.src_dir: return normalized_join(self.src_dir, 'mamafile.py')
        return None


    def mamafile_exists(self):
        return os.path.exists(self.mamafile_path())


    def update_mamafile_tag(self):
        # Shims have no source; the mamafile we'd be tagging doesn't exist on disk.
        # Explicit short-circuit so a future parent-mamafile fetch (Phase 2 target.version
        # probe) doesn't accidentally flag the shim as "modified" every run.
        if self.is_artifactory_shim():
            return False
        return self.src_dir and update_mamafile_tag(self.config, self.mamafile_path(), self.build_dir)


    def update_cmakelists_tag(self):
        if self.is_artifactory_shim():
            return False
        return self.src_dir and update_cmakelists_tag(self.config, self.cmakelists_path(), self.build_dir)


    def build_file_exists(self, filename):
        """ TRUE if a file relative to build_dir exists """
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
        elif os.path.exists(sanitizers_file): # otherwise delete the file, which means sanitizer was not used
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
        """
        Converts relative path into an absolute path based on self mamafile location
        """
        if not relpath or os.path.isabs(relpath):
            return relpath # the path is already None, or Absolute
        elif self.mamafile: # if we have mamafile, set path relative to it
            return normalized_join(os.path.dirname(self.mamafile), relpath)
        else: # otherwise relative to source dir
            if not self.src_dir: # however, artifactory pkgs have no source dir!
                return relpath
            return normalized_join(self.src_dir, relpath)


    def get_mamafile_path_relative_to_us(self, name, relative_mamafile) -> str:
        """
        Converts a relative mamafile path into an absolute path relative to self mamafile location
        """
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
        #console(f'{self.name: <32} last_build: {last_build}')
        #console(f'{self.name: <32} current:    {current}')
        for last in last_build:
            if not (last in current):
                return last.strip()
        return None # Nothing missing


    ## Clean
    def clean(self):
        if self.config.print:
            console(f'  - Target {self.name: <16} CLEAN  {self.config.platform_build_dir_name()}')

        if self.build_dir == '/' or not os.path.exists(self.build_dir):
            return

        self.target.clean() # Customization point
        shutil.rmtree(self.build_dir, ignore_errors=True)


    def dirty(self):
        """ Marks this dependency as dirty in the mamafile_tag """
        if self.config.print: console(f'  - Target {self.name: <16} Dirty')

        if self.target.build_products:
            # make sure we don't have a valid build product to link to
            depfile = self.target.build_products[0]
            if os.path.exists(depfile):
                os.remove(depfile)
                if self.config.verbose: console(f'    dirty: removed {depfile}')

        if self.build_dir_exists():
            # mamafile tag is used to check if mamafile.py has changed
            mamafile_tag = normalized_join(self.build_dir, 'mamafile_tag')
            if os.path.exists(mamafile_tag):
                os.remove(mamafile_tag)
                if self.config.verbose: console('    dirty: removed mamafile_tag')

            # this is needed for artifactory packages
            papafile = self.papa_package_file()
            if os.path.exists(papafile):
                os.remove(papafile)
                if self.config.verbose: console('    dirty: removed papa.txt')

            # remove shim marker so next build re-evaluates artifactory freshness
            self.remove_shim_marker()
