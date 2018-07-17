import os, subprocess, shutil, stat
from mama.parse_mamafile import parse_mamafile, update_mamafile_tag, update_cmakelists_tag
from mama.system import System, console, execute
from mama.util import is_dir_empty, has_tag_changed, write_text_to, read_lines_from, forward_slashes, back_slashes

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
        self.url_changed = self.url != lines[0].rstrip()
        self.tag_changed = self.tag != lines[1].rstrip()
        self.branch_changed = self.branch != lines[2].rstrip()
        self.commit_changed = self.current_commit() != lines[3].rstrip()
        #console(f'check_status {self.url}: urlc={self.url_changed} tagc={self.tag_changed} brnc={self.branch_changed} cmtc={self.commit_changed}')
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
            self.checkout_current_branch()
            if not self.tag: # never pull a tag
                self.run_git("reset --hard")
                self.run_git("pull")

class BuildDependency:
    loaded_deps = dict()
    def __init__(self, name, config, target_class, workspace=None, src=None, git=None, is_root=False, mamafile=None):
        self.name       = name
        self.workspace  = workspace
        self.config     = config
        self.target     = None
        self.target_class = target_class
        self.mamafile     = mamafile
        self.should_rebuild = False
        self.nothing_to_build = False
        self.already_loaded = False
        self.already_executed  = False
        self.is_root = is_root # Root deps are always built
        self.children = []
        self.depends_on = []
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
    def get(name, config, target_class, workspace=None, src=None, git=None, mamafile=None):
        if name in BuildDependency.loaded_deps:
            return BuildDependency.loaded_deps[name]
            
        dependency = BuildDependency(name, config, target_class, \
                        workspace=workspace, src=src, git=git, mamafile=mamafile)
        BuildDependency.loaded_deps[name] = dependency
        return dependency


    def update_dep_dir(self):
        dep_name = self.name
        if self.git:
            if self.git.branch: dep_name = f'{self.name}-{self.git.branch}'
            elif self.git.tag:  dep_name = f'{self.name}-{self.git.tag}'
        self.dep_dir   = forward_slashes(os.path.join(self.config.workspaces_root, self.workspace, dep_name))
        self.build_dir = forward_slashes(os.path.join(self.dep_dir, self.config.name()))
        

    def is_reconfigure_target(self):
        return self.config.configure and self.config.target == self.name

    def exported_libs_file(self):
        return self.build_dir + '/mama_exported_libs'

    def load_build_dependencies(self, target):
        for saved_dependency in read_lines_from(self.exported_libs_file()):
            target.add_build_dependency(saved_dependency)

    def save_exports_as_dependencies(self, exports):
        write_text_to(self.exported_libs_file(), '\n'.join(exports))


    def get_missing_build_dependency(self):
        for depfile in self.target.build_dependencies:
            if not os.path.getsize(depfile):
                return depfile
        return None

    def create_build_dir_if_needed(self):
        if not os.path.exists(self.build_dir): # check to avoid Access Denied errors
            os.makedirs(self.build_dir, exist_ok=True)

    ## @return True if dependency has changed
    def load(self):
        #console(f'LOAD {self.name}')
        git_changed = self.git_checkout()
        self.create_build_target()
        self.update_dep_dir()
        self.create_build_dir_if_needed()

        if git_changed:
            #console(f'SAVE GIT STATUS {self.target.name}')
            self.git.save_status() # save git status to avoid recloning

        target = self.target
        conf = self.config
        is_target = conf.target_matches(target.name)
        if conf.clean and is_target:
            self.clean()

        if not self.is_root:
            self.load_build_dependencies(target)
        target.dependencies() ## customization point for additional dependencies

        build = False
        if conf.build:
            build = True
            def reason(r): console(f'  - Target {target.name: <16}   BUILD [{r}]')

            if conf.target and not is_target:
                build = False # skip build if target doesn't match
            
            elif conf.clean and is_target: reason('cleaned target')
            elif self.is_root:             reason('root target')
            elif update_mamafile_tag(self.src_dir, self.build_dir):   reason(f'{target.name}/mamafile.py modified')
            elif update_cmakelists_tag(self.src_dir, self.build_dir): reason(f'{target.name}/CMakeLists.txt modified')
            elif git_changed:                   reason('git commit changed')
            elif self.is_reconfigure_target():  reason(f'configure target={target.name}')
            elif not target.build_dependencies: reason('no build dependencies')
            else:
                missing = self.get_missing_build_dependency()
                if missing: reason(f'{missing} does not exist')
                else:
                    console(f'  - Target {target.name: <16}   OK')
                    build = False

        self.already_loaded = True
        self.should_rebuild = build
        if build:
            self.create_build_dir_if_needed() # in case we just cleaned
        return build


    def create_build_target(self):
        if self.target:
            return

        project, buildTarget = parse_mamafile(self.config, self.src_dir, \
                                            self.target_class, mamafile=self.mamafile)
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
            self.target = buildTarget(name=project, config=self.config, dep=self)
        else:
            if not self.workspace:
                self.workspace = 'build'
            self.target = self.target_class(name=self.name, config=self.config, dep=self)


    def git_checkout(self):
        if not self.git:
            return False
        
        # if no update command, allow us to skip pulling by returning False
        changed = self.git.check_status()
        is_target = self.config.target_matches(self.name)
        update = self.git.commit_changed or (self.config.update and is_target)
        if not changed and not update:
            return False

        wiped = False
        should_wipe = self.git.url_changed and not self.git.missing_status
        if should_wipe or (is_target and self.config.reclone):
            self.git.reclone_wipe()
            wiped = True
        self.git.clone_or_pull(wiped)
        return True

    ## GIT
    def save_git_status(self):
        if self.git: self.git.save_status()

    ## Clean
    def clean(self):
        console(f'  - Target {self.name: <16}   CLEAN')

        if self.build_dir == '/' or not os.path.exists(self.build_dir):
            return
        
        self.target.clean() # Customization point
        shutil.rmtree(self.build_dir, ignore_errors=True)
