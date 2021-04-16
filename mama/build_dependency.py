import os, subprocess, shutil, stat
from mama.parse_mamafile import parse_mamafile, update_mamafile_tag, update_cmakelists_tag
from mama.system import System, console, execute, execute_piped
from mama.util import is_dir_empty, has_tag_changed, write_text_to, read_lines_from, forward_slashes, back_slashes
from mama.package import cleanup_libs_list
from time import sleep

######################################################################################

class Git:
    def __init__(self, url, branch, tag):
        if not url: raise RuntimeError("Git url must not be empty!")
        self.url = url
        self.branch = branch
        self.tag = tag
        self.dep = None
        self.missing_status = False
        self.url_changed = False
        self.tag_changed = False
        self.branch_changed = False
        self.commit_changed = False


    def run_git(self, git_command):
        cmd = f"cd {self.dep.src_dir} && git {git_command}"
        if self.dep.config.verbose:
            console(f'  {self.dep.name: <16} git {git_command}')
        execute(cmd)


    def fetch_origin(self):
        self.run_git(f"pull origin {self.branch_or_tag()} -q")


    def current_commit(self):
        result = execute_piped(['git', 'show', '--oneline', '-s'], cwd=self.dep.src_dir)
        if self.dep.config.verbose:
            console(f'  {self.dep.name: <16} git show --oneline -s:   {result}')
        return result


    def save_status(self):
        status = f"{self.url}\n{self.tag}\n{self.branch}\n{self.current_commit()}\n"
        write_text_to(f"{self.dep.build_dir}/git_status", status)


    def check_status(self):
        lines = read_lines_from(f"{self.dep.build_dir}/git_status")
        if not lines:
            self.missing_status = True
            if not self.url: return False
            #console(f'check_status {self.url}: NO STATUS AT {self.dep.build_dir}/git_status')
            self.url_changed = True
            self.tag_changed = True
            self.branch_changed = True
            self.commit_changed = True
            return True
        self.fetch_origin()
        self.url_changed = self.url != lines[0].rstrip()
        self.tag_changed = self.tag != lines[1].rstrip()
        self.branch_changed = self.branch != lines[2].rstrip()
        self.commit_changed = self.current_commit() != lines[3].rstrip()
        #console(f'check_status {self.url} {self.branch_or_tag()}: urlc={self.url_changed} tagc={self.tag_changed} brnc={self.branch_changed} cmtc={self.commit_changed}')
        return self.url_changed or self.tag_changed or self.branch_changed or self.commit_changed


    def branch_or_tag(self):
        if self.branch: return self.branch
        if self.tag: return self.tag
        return ''


    def checkout_current_branch(self):
        branch = self.branch_or_tag()
        if branch:
            if self.tag and self.tag_changed:
                self.run_git("reset --hard")
            self.run_git(f"checkout {branch}")


    def reclone_wipe(self):
        if self.dep.config.print:
            console(f'  - Target {self.dep.name: <16}   RECLONE WIPE')
        if os.path.exists(self.dep.dep_dir):
            if System.windows: # chmod everything to user so we can delete:
                for root, dirs, files in os.walk(self.dep.dep_dir):
                    for d in dirs:  os.chmod(os.path.join(root, d), stat.S_IWUSR)
                    for f in files: os.chmod(os.path.join(root, f), stat.S_IWUSR)
            shutil.rmtree(self.dep.dep_dir)


    def clone_or_pull(self, wiped=False):
        if is_dir_empty(self.dep.src_dir):
            if not wiped and self.dep.config.print:
                console(f"  - Target {self.dep.name: <16}   CLONE because src is missing")
            branch = self.branch_or_tag()
            if branch: branch = f" --branch {self.branch_or_tag()}"
            execute(f"git clone --recurse-submodules --depth 1 {branch} {self.url} {self.dep.src_dir}", self.dep.config.verbose)
            self.checkout_current_branch()
        else:
            if self.dep.config.print:
                console(f"  - Pulling {self.dep.name: <16}  SCM change detected")
            self.checkout_current_branch()
            execute("git submodule update --init --recursive")
            if not self.tag: # pull if not a tag
                self.run_git("reset --hard -q")
                self.run_git("pull")


class BuildDependency:
    loaded_deps = dict()
    def __init__(self, name, config, target_class, workspace=None, src=None, git=None, \
                       is_root=False, mamafile=None, always_build=False, args=[]):
        self.name       = name
        self.workspace  = workspace
        self.config     = config
        self.target     = None
        self.target_class = target_class
        self.target_args  = args
        self.mamafile     = mamafile
        self.always_build    = always_build
        self.should_rebuild    = False
        self.nothing_to_build  = False
        self.already_loaded    = False
        self.already_executed  = False
        self.currently_loading = False
        self.is_root         = is_root # Root deps are always built
        self.children        = []
        self.depends_on      = []
        self.product_sources = []
        self.flattened_deps = [] # used for debugging
        if not src and not git:
            raise RuntimeError(f'{name} src and git not configured. Specify at least one.')

        if git:
            self.git     = git
            git.dep      = self
            self.update_dep_dir()
            self.src_dir = forward_slashes(os.path.join(self.dep_dir, self.name))
            self.target  = None
        else:
            self.git     = None
            self.src_dir = forward_slashes(src)
            self.create_build_target()
            self.name = self.target.name
            self.update_dep_dir()
    
    
    @staticmethod
    def get(name, config, target_class, workspace, src=None, git=None, \
            mamafile=None, always_build=False, args=[]):
        if name in BuildDependency.loaded_deps:
            #console(f'Using existing BuildDependency {name}')
            dependency = BuildDependency.loaded_deps[name]
            dependency.target_args += args
            if dependency.target:
                dependency.target._set_args(args)
            return dependency
        
        dependency = BuildDependency(name, config, target_class, \
                        workspace=workspace, src=src, git=git, mamafile=mamafile,
                        always_build=always_build, args=args)
        BuildDependency.loaded_deps[name] = dependency
        return dependency


    def update_dep_dir(self):
        dep_name = self.name
        if self.git:
            if self.git.branch: dep_name = f'{self.name}-{self.git.branch}'
            elif self.git.tag:  dep_name = f'{self.name}-{self.git.tag}'
        self.dep_dir   = forward_slashes(os.path.join(self.config.workspaces_root, self.workspace, dep_name))
        self.build_dir = forward_slashes(os.path.join(self.dep_dir, self.config.build_folder()))


    def has_build_files(self):
        return os.path.exists(self.build_dir+'/CMakeCache.txt') \
            or os.path.exists(self.build_dir+'/Makefile')


    def exported_libs_file(self):
        return self.build_dir + '/mama_exported_libs'


    def load_build_dependencies(self, target):
        loaded_deps = read_lines_from(self.exported_libs_file())
        loaded_deps = cleanup_libs_list(loaded_deps)
        if loaded_deps:
            target.build_dependencies += loaded_deps


    def save_exports_as_dependencies(self, exports):
        write_text_to(self.exported_libs_file(), '\n'.join(exports))


    def find_first_missing_build_product(self):
        for depfile in self.target.build_dependencies:
            if not os.path.exists(depfile):
                return depfile
        return None


    def source_dir_exists(self):
        return os.path.exists(self.src_dir)


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
                sleep(0.1)
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
        if not self.git or self.is_root:    # No git for local or root targets
            return False

        if not self.source_dir_exists():  # we MUST pull here
            self.git.clone_or_pull()
            return True

        changed = self.git.check_status() if self.config.update else False
        is_target = self.config.target_matches(self.name)

        wiped = False
        should_wipe = self.git.url_changed and not self.git.missing_status
        if should_wipe or (is_target and self.config.reclone):
            self.git.reclone_wipe()
            wiped = True
        else:
            # don't pull if no changes to git status
            # or if we're current target of a non-update build
            # mama update target=ReCpp  -- this should git pull
            # mama build target=ReCpp   -- should NOT pull
            non_update_target = is_target and not self.config.update
            if non_update_target or not changed:
                return False

        self.git.clone_or_pull(wiped)
        return True


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

        if not self.is_root:
            self.load_build_dependencies(target)
        
        target.dependencies() ## customization point for additional dependencies

        build = False
        if conf.build or conf.update:
            build = self._should_build(conf, target, is_target, git_changed)
            if build:
                self.create_build_dir_if_needed() # in case we just cleaned
            if git_changed:
                self.git.save_status()

        self.already_loaded = True
        self.should_rebuild = build
        if conf.list:
            self._print_list(conf, target)
        return build


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
            if conf.clean and is_target:     return build('cleaned target')
            if self.is_root:                 return build('root target')
            if self.always_build:            return build('always build')
            if git_changed:                  return build('git commit changed')
            if   update_mamafile_tag(self.mamafile_path(),   self.build_dir): return build(target.name+'/mamafile.py modified')
            if update_cmakelists_tag(self.cmakelists_path(), self.build_dir): return build(target.name+'/CMakeLists.txt modified')

            if not self.nothing_to_build:
                if not self.has_build_files():    return build('not built yet')
                if not target.build_dependencies: return build('no build dependencies')

            missing_product = self.find_first_missing_build_product()
            if missing_product: return build(f'{missing_product} does not exist')
            missing_dep = self.find_missing_dependency()
            if missing_dep: return build(f'{missing_dep} was removed')

            # Finally, if we call `update this_target`
            if conf.update and conf.target == target.name:
                return build('update target='+conf.target)

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
        update_mamafile_tag(self.mamafile_path(), self.build_dir)
        update_cmakelists_tag(self.cmakelists_path(), self.build_dir)
        self.save_dependency_list()
        if self.git:
            self.git.save_status()


    def create_build_target(self):
        if self.target:
            self.target._set_args(self.target_args)
            return

        project, buildTarget = parse_mamafile(self.config, self.target_class, self.mamafile_path())
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
            self.target = self.target_class(name=self.name, config=self.config, dep=self, args=self.target_args)


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
        return self.mamafile if self.mamafile else os.path.join(self.src_dir, 'mamafile.py')


    def mamafile_exists(self):
        return os.path.exists(self.mamafile_path())


    # "name(-branch)"
    def get_dependency_name(self):
        if self.git:
            branch = self.git.branch_or_tag()
            if branch:
                return self.name + '-' + branch
        return self.name


    def save_dependency_list(self):
        deps = [dep.get_dependency_name() for dep in self.children]
        write_text_to(f'{self.build_dir}/mama_dependency_libs', '\n'.join(deps))


    def find_missing_dependency(self):
        last_build = [dep.rstrip() for dep in read_lines_from(f'{self.build_dir}/mama_dependency_libs')]
        current = [dep.get_dependency_name() for dep in self.children]
        #console(f'{self.name: <32} last_build: {last_build}')
        #console(f'{self.name: <32} current:    {current}')
        for last in last_build:
            if not (last in current):
                return last.strip()
        return None # Nothing missing


    ## Clean
    def clean(self):
        if self.config.print:
            console(f'  - Target {self.name: <16}   CLEAN  {self.config.build_folder()}')

        if self.build_dir == '/' or not os.path.exists(self.build_dir):
            return
        
        self.target.clean() # Customization point
        shutil.rmtree(self.build_dir, ignore_errors=True)
