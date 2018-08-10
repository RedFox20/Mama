import os, subprocess, shutil, stat
from mama.parse_mamafile import parse_mamafile, update_mamafile_tag, update_cmakelists_tag
from mama.system import System, console, execute
from mama.util import is_dir_empty, has_tag_changed, write_text_to, read_lines_from, forward_slashes, back_slashes
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
        #console(cmd)
        execute(cmd)

    def fetch_origin(self):
        self.run_git(f"pull origin {self.branch_or_tag()} -q")

    def current_commit(self):
        cp = subprocess.run(['git','show','--oneline','-s'], stdout=subprocess.PIPE, cwd=self.dep.src_dir)
        return cp.stdout.decode('utf-8').rstrip()

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
        console(f'  - Target {self.dep.name: <16}   RECLONE WIPE')
        if os.path.exists(self.dep.dep_dir):
            if System.windows: # chmod everything to user so we can delete:
                for root, dirs, files in os.walk(self.dep.dep_dir):
                    for d in dirs:  os.chmod(os.path.join(root, d), stat.S_IWUSR)
                    for f in files: os.chmod(os.path.join(root, f), stat.S_IWUSR)
            shutil.rmtree(self.dep.dep_dir)

    def clone_or_pull(self, wiped=False):
        if is_dir_empty(self.dep.src_dir):
            if not wiped:
                console(f"  - Target {self.dep.name: <16}   CLONE because src is missing")
            branch = self.branch_or_tag()
            if branch: branch = f" --branch {self.branch_or_tag()}"
            execute(f"git clone --depth 1 {branch} {self.url} {self.dep.src_dir}")
            self.checkout_current_branch()
        else:
            console(f"  - Pulling {self.dep.name: <16}  SCM change detected")
            self.checkout_current_branch()
            if not self.tag: # never pull a tag
                self.run_git("reset --hard -q")
                self.run_git("pull")

class BuildDependency:
    loaded_deps = dict()
    def __init__(self, name, config, target_class, workspace=None, src=None, git=None, is_root=False, mamafile=None, args=[]):
        self.name       = name
        self.workspace  = workspace
        self.config     = config
        self.target     = None
        self.target_class = target_class
        self.target_args  = args
        self.mamafile     = mamafile
        self.should_rebuild    = False
        self.nothing_to_build  = False
        self.already_loaded    = False
        self.already_executed  = False
        self.currently_loading = False
        self.is_root         = is_root # Root deps are always built
        self.children        = []
        self.depends_on      = []
        self.product_sources = []
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
    def get(name, config, target_class, workspace, src=None, git=None, mamafile=None, args=[]):
        if name in BuildDependency.loaded_deps:
            #console(f'Using existing BuildDependency {name}')
            dependency = BuildDependency.loaded_deps[name]
            dependency.target_args += args
            if dependency.target:
                dependency.target.set_args(args)
            return dependency
        
        dependency = BuildDependency(name, config, target_class, \
                        workspace=workspace, src=src, git=git, mamafile=mamafile, args=args)
        BuildDependency.loaded_deps[name] = dependency
        return dependency


    def update_dep_dir(self):
        dep_name = self.name
        if self.git:
            if self.git.branch: dep_name = f'{self.name}-{self.git.branch}'
            elif self.git.tag:  dep_name = f'{self.name}-{self.git.tag}'
        self.dep_dir   = forward_slashes(os.path.join(self.config.workspaces_root, self.workspace, dep_name))
        self.build_dir = forward_slashes(os.path.join(self.dep_dir, self.config.name()))


    def has_build_files(self):
        return os.path.exists(self.build_dir+'/CMakeCache.txt') \
            or os.path.exists(self.build_dir+'/Makefile')


    def exported_libs_file(self):
        return self.build_dir + '/mama_exported_libs'


    def load_build_dependencies(self, target):
        for saved_dependency in read_lines_from(self.exported_libs_file()):
            saved_dependency = saved_dependency.strip()
            target.build_dependencies.append(saved_dependency)


    def save_exports_as_dependencies(self, exports):
        write_text_to(self.exported_libs_file(), '\n'.join(exports))


    def get_missing_build_dependency(self):
        frameworks = self.config.ios or self.config.macos
        for depfile in self.target.build_dependencies:
            if frameworks and depfile.startswith('-framework'):
                continue
            if not os.path.getsize(depfile):
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
        
        if not self.source_dir_exists():
            self.git.clone_or_pull()
            return True

        changed = self.git.check_status()
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
        self.create_build_target()
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
            build = True
            def target_args(): return f'{target.args}' if target.args else ''
            def reason(r): console(f'  - Target {target.name: <16}   BUILD [{r}]  {target_args()}')

            if conf.target and not is_target: # if we called: "target=SpecificProject"
                build = False # skip build if target doesn't match
            
            elif conf.clean and is_target:  reason('cleaned target')
            elif self.is_root:              reason('root target')
            elif conf.configure and is_target: reason('configure target='+target.name)
            elif conf.target    and is_target: reason('target='+target.name)
            elif   update_mamafile_tag(self.mamafile_path(), self.build_dir): reason(target.name+'/mamafile.py modified')
            elif update_cmakelists_tag(self.cmakelists_path(), self.build_dir): reason(target.name+'/CMakeLists.txt modified')
            elif git_changed:                   reason('git commit changed')
            elif not self.has_build_files():    reason('not built yet')
            elif not target.build_dependencies: reason('no build dependencies')
            else:
                missing = self.get_missing_build_dependency()
                if missing: reason(f'{missing} does not exist')
                else:
                    console(f'  - Target {target.name: <16}   OK')
                    build = False
            
            if not build and git_changed:
                self.git.save_status()

        self.already_loaded = True
        self.should_rebuild = build
        if build:
            self.create_build_dir_if_needed() # in case we just cleaned
        return build


    def after_load(self):
        first_changed = next((c for c in self.children if c.should_rebuild), None)
        if first_changed and not self.should_rebuild:
            self.should_rebuild = True
            console(f'  - Target {self.name: <16}  BUILD [{first_changed.name} changed]')
            self.create_build_dir_if_needed() # in case we just cleaned


    def successful_build(self):
        update_mamafile_tag(self.mamafile_path(), self.build_dir)
        update_cmakelists_tag(self.cmakelists_path(), self.build_dir)
        if self.git:
            self.git.save_status()


    def create_build_target(self):
        if self.target:
            self.target.set_args(self.target_args)
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


    ## Clean
    def clean(self):
        console(f'  - Target {self.name: <16}   CLEAN')

        if self.build_dir == '/' or not os.path.exists(self.build_dir):
            return
        
        self.target.clean() # Customization point
        shutil.rmtree(self.build_dir, ignore_errors=True)
