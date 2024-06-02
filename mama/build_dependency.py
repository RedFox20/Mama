from __future__ import annotations
from typing import List, TYPE_CHECKING
import os, sys, shutil, time

from .types.dep_source import DepSource
from .types.git import Git
from .types.local_source import LocalSource
from .utils.system import Color, console, error
from .artifactory import artifactory_fetch_and_reconfigure
from .util import normalized_join, normalized_path, read_text_from, write_text_to, read_lines_from
from .parse_mamafile import parse_mamafile, update_mamafile_tag, update_cmakelists_tag
import mama.package as package


if TYPE_CHECKING:
    from .build_config import BuildConfig
    from .build_target import BuildTarget


######################################################################################


class BuildDependency:
    loaded_deps = dict()
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
        self.from_artifactory = False # if true, this Dependency was loaded from Artifactory
        self.did_check_artifactory = False # if true, artifactory was already checked and can be skipped
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


    @staticmethod
    def get_loaded_dependency(name: str) -> BuildDependency:
        if name in BuildDependency.loaded_deps:
            return BuildDependency.loaded_deps[name]
        return None


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
        Adds a new child dependency to this BuildDependency
        """
        dep = BuildDependency.get_loaded_dependency(dep_source.name)
        if dep:
            # reuse & update existing dep
            dep.update_existing_dependency(dep_source)
        else:
            # add new
            dep = BuildDependency(self, self.config, self.workspace, dep_source)
            BuildDependency.loaded_deps[dep_source.name] = dep
            if self.config.verbose:
                console(f'  - Target {self.name: <16} ADD {dep}', color=Color.BLUE)

        if dep in self.children:
            raise RuntimeError(f"BuildTarget {self.name} add dependency '{dep.name}'"\
                                " failed because it has already been added")

        self.children.append(dep)
        return dep


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


    def find_first_missing_build_product(self):
        for depfile in self.target.build_products:
            if not os.path.exists(depfile):
                return depfile
        return None


    def source_dir_exists(self):
        return self.src_dir and os.path.exists(self.src_dir)


    def build_dir_exists(self):
        return os.path.exists(self.build_dir)


    def create_build_dir_if_needed(self):
        if not os.path.exists(self.build_dir): # check to avoid Access Denied errors
            os.makedirs(self.build_dir, exist_ok=True)


    ## @return True if dependency has changed
    def load(self):
        if self.currently_loading:
            #console(f'WAIT {self.name}')
            while self.currently_loading:
                time.sleep(0.1)
            return self.should_rebuild
        #console(f'LOAD {self.name}')
        changed = False
        try:
            self.currently_loading = True
            changed = self._load()
        finally:
            self.currently_loading = False
        return changed


    def _load_target(self) -> BuildTarget:
        self.create_build_target() ## parses target mamafile
        self._update_dep_name_and_dirs(self.name) ## requires target mamafile workspace
        self.create_build_dir_if_needed()
        return self.target

    def _git_checkout_if_needed(self) -> bool:
        if not self.is_root and self.dep_source.is_git:
            git:Git = self.dep_source
            return git.dependency_checkout(self)
        return False


    def _load(self):
        conf = self.config
        if conf.verbose:
            console(f'  - Target {self.name: <16} LOAD ({self.dep_source.get_type_string()})', color=Color.BLUE)

        is_target = self.is_current_target()

        # for root targets, always load the BuildTarget immediately, we need the root workspace from its mamafile
        if self.is_root:
            target = self._load_target()
        # for non-root targets, only create the required dirs
        else:
            self._update_dep_name_and_dirs(self.name)
            self.create_build_dir_if_needed()

        git_changed = self._git_checkout_if_needed() ## pull Git before loading target Mamafile

        target = self._load_target() ## load target for Git and Src

        if conf.clean and is_target:
            self.clean() ## requires a parsed mamafile target

        # if artifactory_fetch_and_reconfigure succeeds, it will overwrite products and libs
        # and sets self.from_artifactory
        loaded_from_pkg = False
        should_load_art = self.should_load_artifactory()
        if should_load_art and self.can_fetch_artifactory(print=True, which='LOAD'):
            self.did_check_artifactory = True
            fetched, dependencies = artifactory_fetch_and_reconfigure(target)
            if fetched:
                for dep_name in dependencies:
                    self.add_child(dep_name)
                loaded_from_pkg = True
            elif self.dep_source.is_pkg:
                raise RuntimeError(f'  - Target {self.name} failed to load artifactory pkg {self.dep_source}')
        elif should_load_art and self.is_force_art_target():
            raise RuntimeError(f'  - Target {self.name} failed to find artifactory pkg {self.dep_source} but `art` was specified')

        # load any build products from previous builds
        if not self.is_root and not loaded_from_pkg:
            self.load_build_products(target)

        if conf.verbose:
            console(f'  - Target {self.name: <16} load settings and dependencies')
        target.settings() ## customization point for project settings
        target.dependencies() ## customization point for additional dependencies

        if not loaded_from_pkg and self.is_root:
            # fetch the compiler immediately from root settings
            conf.get_preferred_compiler_paths()

        build = False
        if conf.build or conf.update:
            build = self._should_build(conf, target, is_target, git_changed, loaded_from_pkg)
            if build:
                self.create_build_dir_if_needed() # in case we just cleaned
            if git_changed:
                git:Git = self.dep_source
                git.save_status(self)

        self.already_loaded = True
        self.should_rebuild = build
        if conf.list:
            self._print_list(conf, target)
        return build


    def can_fetch_artifactory(self, print: bool, which: str):
        if self.is_root or self.did_check_artifactory:
            return False

        force_art = self.config.force_artifactory
        disable_art = self.config.disable_artifactory
        is_target = self.is_current_target()

        def noart(r):
            if print and (self.config.print or force_art):
                console(f'  - Target {self.name: <16} NO ARTIFACTORY PKG [{which} {r}]', color=Color.YELLOW)
            self.did_check_artifactory = True
            return False

        if disable_art:
            return noart('noart override')
        elif is_target and not force_art:
            # don't load during rebuild -- defer to source based builds in that case
            if self.config.rebuild: return noart('target rebuild')
            # don't load anything during cleaning -- because it will get cleaned anyways
            if self.config.clean: return noart('target clean')
        elif print and (self.config.verbose or force_art):
            console(f'  - Target {self.name: <16} CHECK ARTIFACTORY PKG [{which}]', color=Color.YELLOW)

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
                args = f'{target.args}' if target.args else ''
                console(f'  - Target {target.name: <16} BUILD [{r}]  {args}', color=Color.YELLOW)
            return True

        if conf.target and not is_target: # if we called: "target=SpecificProject"
            return False # skip build if target doesn't match

        ## build also entails packaging
        if conf.clean and is_target: return build('cleaned target')
        if self.is_root:             return build('root target')
        if self.always_build:        return build('always build')
        if git_changed:              return build('git commit changed')
        if self.dep_source.is_pkg:   return build('artifactory pkg')

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


    def create_build_target(self):
        if self.target:
            self.target._set_args(self.target_args)
            return

        # load the default mama.BuildTarget class
        mamaBuildTarget = getattr(sys.modules['mama.build_target'], 'BuildTarget')
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
                else:                                    self.workspace = 'build'
            if self.is_root:
                if   'workspace'        in buildStatics: self.config.global_workspace = False
                elif 'local_workspace'  in buildStatics: self.config.global_workspace = False
                elif 'global_workspace' in buildStatics: self.config.global_workspace = True
                if not self.config.global_workspace:
                    self.config.workspaces_root = self.src_dir
            self.target = buildTarget(name=project, config=self.config, dep=self, args=self.target_args)
        else:
            if not self.workspace:
                self.workspace = 'build'
            if self.config.verbose:
                console(f'  - Target {self.name: <16} Using Default BuildTarget Project={project} BuildTarget={buildTarget}', color=Color.YELLOW)
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
        return self.src_dir and update_mamafile_tag(self.config, self.mamafile_path(), self.build_dir)


    def update_cmakelists_tag(self):
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
