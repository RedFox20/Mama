import os, shutil, time
import sys
from typing import List

from .types.dep_source import DepSource
from .types.git import Git
from .types.local_source import LocalSource
from .utils.system import console
from .artifactory import artifactory_fetch_and_reconfigure
from .util import normalized_join, normalized_path, write_text_to, read_lines_from
from .parse_mamafile import parse_mamafile, update_mamafile_tag, update_cmakelists_tag
import mama.package as package


######################################################################################


class BuildDependency:
    loaded_deps = dict()
    def __init__(self, parent:"BuildDependency", config,
                 workspace, dep_source:DepSource):
        self.config = config
        self.workspace = workspace
        self.mamafile = None
        self.target = None
        self.target_args = []
        self.always_build = False
        self.should_rebuild = False
        self.nothing_to_build = False
        self.already_loaded = False
        self.already_executed = False
        self.currently_loading = False
        self.from_artifactory = False # if true, this Dependency was loaded from Artifactory
        self.is_root = parent is None # Root deps are always built
        self.children = []
        self.depends_on = []
        self.product_sources = []
        self.flattened_deps = [] # used for debugging

        self.src_dir = None # source directory where the code is located
        self.dep_dir = None # dependency dir where platform build dirs are kept
        self.build_dir = None # {dep_dir}/{config.platform_name()}
        self.dep_source = dep_source
        self.name = dep_source.name

        if dep_source.is_git:
            git:Git = dep_source
            self.mamafile = git.mamafile # git.mamafile is the relative path
            if parent:
                self.mamafile = parent.get_mamafile_path_relative_to_us(self.name, git.mamafile)
            self.target_args = git.args
            self.update_dep_dir()
            # put the git repo in workspace
            self.src_dir = normalized_join(self.dep_dir, self.name)
        elif dep_source.is_pkg:
            if not config.artifactory_ftp:
                raise RuntimeError(f'add_artifactory_pkg({self.name}) failed because config.artifactory_ftp is not set!')
            self.src_dir = None # there is no src_dir when using artifactory packages
            self.create_build_target()
            self.update_dep_dir()
        elif dep_source.is_src:
            src:LocalSource = dep_source
            self.mamafile = src.mamafile
            self.target_args = src.args
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
            self.name = str(self.target.name) # might change due to mamafile ??
            self.update_dep_dir()
        else:
            raise RuntimeError(f'{self.name} src or git or pkg not configured. Specify at least one.')


    @staticmethod
    def get_loaded_dependency(name:str) -> "BuildDependency":
        if name in BuildDependency.loaded_deps:
            return BuildDependency.loaded_deps[name]
        return None


    def update_existing_dependency(self, dep_source:DepSource):
        if dep_source.is_git or dep_source.is_src:
            self.target_args += dep_source.args
            if self.target:
                self.target._set_args(self.target_args)


    def add_child(self, dep_source:DepSource) -> "BuildDependency":
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

        if dep in self.children:
            raise RuntimeError(f"BuildTarget {self.name} add dependency '{dep.name}'"\
                                " failed because it has already been added")

        self.children.append(dep)


    def get_children(self) -> List["BuildDependency"]:
        """ Gets already resolved dependencies """
        if self.children is None:
            raise RuntimeError(f'Target {self.name} child dependencies unresolved')
        return self.children


    def update_dep_dir(self):
        dep_name = self.name
        if self.dep_source.is_git:
            git:Git = self.dep_source
            if git.branch: dep_name = f'{self.name}-{git.branch}'
            elif git.tag: dep_name = f'{self.name}-{git.tag}'
        self.dep_dir = normalized_join(self.config.workspaces_root, self.workspace, dep_name)
        self.build_dir = normalized_join(self.dep_dir, self.config.platform_name())


    def has_build_files(self):
        return os.path.exists(self.build_dir+'/CMakeCache.txt') \
            or os.path.exists(self.build_dir+'/Makefile')


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


    def git_checkout(self):
        # ignore non-git or root targets
        if not self.dep_source.is_git or self.is_root:
            return False
        git:Git = self.dep_source
        return git.dependency_checkout(self)


    def _load(self):
        git_changed = self.git_checkout()
        self.create_build_target()  ## parses target mamafile
        self.update_dep_dir()
        self.create_build_dir_if_needed()

        target = self.target
        conf = self.config
        is_target = conf.target_matches(target.name)
        if conf.clean and is_target:
            self.clean()

        # load any build products from previous builds
        if not self.is_root:
            self.load_build_products(target)

        # if this succeeds, it will overwrite products and libs
        loaded_from_pkg = self.load_artifactory_package(target)
        if not loaded_from_pkg:
            target.dependencies() ## customization point for additional dependencies

        build = False
        if conf.build or conf.update:
            build = self._should_build(conf, target, is_target, git_changed)
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


    def load_artifactory_package(self, target):
        load_art = self.dep_source.is_pkg or os.path.exists(self.papa_package_file())
        if load_art:
            fetched, dependencies = artifactory_fetch_and_reconfigure(target)
            if fetched:
                for dep_name in dependencies:
                    self.add_child(dep_name)
                return True
            elif self.dep_source.is_pkg:
                raise RuntimeError(f'  - Target {target.name} failed to load artifactory pkg {self.dep_source}')
        return False


    def _print_list(self, conf, target):
        if conf.print:
            console(f'  - Target {target.name: <16}')


    def _should_build(self, conf, target, is_target, git_changed):
            def build(r):
                if conf.print:
                    args = f'{target.args}' if target.args else ''
                    console(f'  - Target {target.name: <16}   BUILD [{r}]  {args}')
                return True

            if conf.target and not is_target: # if we called: "target=SpecificProject"
                return False # skip build if target doesn't match

            ## build also entails packaging
            if conf.clean and is_target: return build('cleaned target')
            if self.is_root:             return build('root target')
            if self.always_build:        return build('always build')
            if git_changed:              return build('git commit changed')
            if self.dep_source.is_pkg:   return build('artifactory pkg')
            if self.update_mamafile_tag(): return build(target.name+'/mamafile.py modified')
            if self.update_cmakelists_tag(): return build(target.name+'/CMakeLists.txt modified')

            # if we call `update this_target`
            if conf.update and conf.target == target.name:
                return build('update target='+conf.target)

            # if the project has been built at least once or downloaded from artifactory package
            # then there will be a list of build products
            # if any of those are missing, then this needs to be rebuilt to re-acquire them
            missing_product = self.find_first_missing_build_product()
            if missing_product:
                return build(f'{missing_product} does not exist')

            # project has not defined `nothing_to_build` which is for header-only projects
            # thus we need to check if build should execute
            can_build = not self.nothing_to_build
            if can_build:
                # there are no build products defined at all, it hasn't been built or downloaded
                if not target.build_products:
                    if not self.has_build_files(): return build('not built yet')
                    return build('no build dependencies')

                # we have build products, and none of them are missing
                if target.build_products and not missing_product:
                    pass # added this condition for clarity -- all should be OK

            # something changed in the mamafile, or artifactory package
            # and the list of dependency targets changed, thus we need to rebuild
            missing_dep = self.find_missing_dependency()
            if missing_dep: return build(f'{missing_dep} was removed')

            if conf.print:
                console(f'  - Target {target.name: <16}   OK')
            return False # do not build, all is ok


    def after_load(self):
        if self.config.no_specific_target():
            first_changed = next((c for c in self.children if c.should_rebuild), None)
            if first_changed and not self.should_rebuild:
                self.should_rebuild = True
                if self.config.print:
                    console(f'  - Target {self.name: <16}   BUILD [{first_changed.name} changed]')
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

        # this will load the specific `<class project(mama.build_target)>` class
        project, buildTarget = parse_mamafile(self.config, mamaBuildTarget, self.mamafile_path())
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
            self.target = mamaBuildTarget(name=self.name, config=self.config, dep=self, args=self.target_args)


    def is_root_or_config_target(self):
        if self.config.target:
            return self.config.target_matches(self.name)
        return self.is_root


    def cmakelists_path(self):
        return os.path.join(self.src_dir, 'CMakeLists.txt')


    def cmakelists_exists(self):
        return os.path.exists(self.cmakelists_path())


    def ensure_cmakelists_exists(self):
        if not os.path.exists(self.cmakelists_path()):
            raise IOError(f'Could not find {self.cmakelists_path()}! Add a CMakelists.txt, or add `self.nothing_to_build()` to configuration step. Also note that filename CMakeLists.txt is case sensitive.')


    def mamafile_path(self):
        if self.mamafile: return self.mamafile
        if self.src_dir: return os.path.join(self.src_dir, 'mamafile.py')
        return None


    def mamafile_exists(self):
        return os.path.exists(self.mamafile_path())


    def update_mamafile_tag(self):
        return self.src_dir and update_mamafile_tag(self.mamafile_path(), self.build_dir)


    def update_cmakelists_tag(self):
        return self.src_dir and update_cmakelists_tag(self.cmakelists_path(), self.build_dir)


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
            console(f'  - Target {self.name: <16}   CLEAN  {self.config.platform_name()}')

        if self.build_dir == '/' or not os.path.exists(self.build_dir):
            return
        
        self.target.clean() # Customization point
        shutil.rmtree(self.build_dir, ignore_errors=True)

