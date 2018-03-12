import os, subprocess, shutil, stat
from mama.parse_mamafile import parse_mamafile, update_mamafile_tag, update_cmakelists_tag
from mama.system import System, console, execute
from mama.util import is_dir_empty, has_tag_changed, write_text_to, read_lines_from, forward_slashes

######################################################################################

class Git:
    def __init__(self, url, branch, tag):
        if not url: raise RuntimeError("Git url must not be empty!")
        self.url = url
        self.branch = branch
        self.tag = tag
        self.dep = None

    def run_git(self, git_command):
        cmd = f"cd {self.dep.src_dir} && git {git_command}"
        #console(cmd)
        execute(cmd)

    def current_commit(self): 
        cp = subprocess.run(['git','show','--oneline','-s'], stdout=subprocess.PIPE, cwd=self.dep.src_dir)
        return cp.stdout.decode('utf-8')

    def tag_changed(self):
        return has_tag_changed(f"{self.dep.build_dir}/git_tag", self.tag)

    def commit_changed(self):
        return not os.path.exists(self.dep.build_dir) or\
         has_tag_changed(f"{self.dep.build_dir}/git_commit", self.current_commit())

    def save_tag(self):
        write_text_to(f"{self.dep.build_dir}/git_tag", self.tag)

    def save_commit(self):
        write_text_to(f"{self.dep.build_dir}/git_commit", self.current_commit())

    def checkout_current_branch(self):
        branch = self.branch if self.branch else self.tag
        if branch:
            if self.tag and self.tag_changed():
                self.run_git("reset --hard")
                self.save_tag()
            self.run_git(f"checkout {branch}")

    def reclone_wipe(self):
        console(f'Reclone wipe {self.dep.dep_dir}')
        if os.path.exists(self.dep.dep_dir):
            if System.windows: # chmod everything to user so we can delete:
                for root, dirs, files in os.walk(self.dep.dep_dir):
                    for d in dirs:  os.chmod(os.path.join(root, d), stat.S_IWUSR)
                    for f in files: os.chmod(os.path.join(root, f), stat.S_IWUSR)
            shutil.rmtree(self.dep.dep_dir)

    def clone_or_pull(self):
        if is_dir_empty(self.dep.src_dir):
            console(f"  - Target {self.dep.name: <16}   CLONE because src is missing")
            execute(f"git clone {self.url} {self.dep.src_dir}")
            self.checkout_current_branch()
        else:
            self.checkout_current_branch()
            if not self.tag: # never pull a tag
                self.run_git("reset --hard")
                self.run_git("pull")

class BuildDependency:
    def __init__(self, name, config, target_class, workspace=None, src=None, git=None):
        self.name       = name
        self.workspace  = workspace
        self.config     = config
        self.target     = None
        self.target_class = target_class
        self.should_rebuild = True
        self.is_root = False # Root deps are always built
        self.children   = []
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
            if not os.path.exists(depfile):
                return depfile
        return None

    ## @return True if dependency has changed
    def load(self):
        git_changed = self.git_checkout()
        self.create_build_target()
        self.update_dep_dir()

        if not os.path.exists(self.build_dir): # check to avoid Access Denied errors
            os.makedirs(self.build_dir, exist_ok=True)

        target = self.target
        if not self.is_root:
            self.load_build_dependencies(target)
        target.dependencies() ## customization point for additional dependencies

        conf = self.config
        if conf.clean and (conf.target == 'all' or conf.target == target.name):
            self.clean()

        changed = True
        if conf.build:
            if conf.target and conf.target != 'all' and conf.target != target.name:
                changed = False # skip build if target doesn't match
            elif self.is_root:
                console(f'  - Target {target.name: <16}   BUILD [root target]')
            elif update_mamafile_tag(self.src_dir, self.build_dir):
                console(f'  - Target {target.name: <16}   BUILD [{target.name}/mamafile.py modified]')
            elif update_cmakelists_tag(self.src_dir, self.build_dir):
                console(f'  - Target {target.name: <16}   BUILD [{target.name}/CMakeLists.txt modified]')
            elif git_changed:
                console(f'  - Target {target.name: <16}   BUILD [git commit changed]')
            elif self.is_reconfigure_target():
                console(f'  - Target {target.name: <16}   BUILD [configure target={target.name}')
            elif not target.build_dependencies:
                console(f'  - Target {target.name: <16}   BUILD [no build dependencies]')
            else:
                missing = self.get_missing_build_dependency()
                if missing:
                    console(f'  - Target {target.name: <16}   BUILD [{missing} does not exist]')
                else:
                    console(f'  - Target {target.name: <16}   OK')
                    changed = False

        self.should_rebuild = changed
        return changed

    def create_build_target(self):
        if self.target:
            return

        project, buildTarget = parse_mamafile(self.config, self.src_dir, self.target_class)
        
        if project and buildTarget:
            buildStatics = buildTarget.__dict__
            if not self.workspace:
                self.workspace = buildStatics['workspace'] if 'workspace' in buildStatics else 'mamabuild'
            self.target = buildTarget(name=project, config=self.config, dep=self)
        else:
            if not self.workspace:
                self.workspace = 'mamabuild'
            self.target = self.target_class(name=self.name, config=self.config, dep=self)


    def git_checkout(self):
        if not self.git or not self.git.commit_changed():
            return False
        if self.config.reclone and self.config.target == self.name:
            self.git.reclone_wipe()
        self.git.clone_or_pull()
        return True

    ## GIT
    def save_git_commit(self):
        if self.git: self.git.save_commit()

    ## Clean
    def clean(self):
        console(f'  - Target {self.name: <16}   CLEAN')

        if self.build_dir == '/' or not os.path.exists(self.build_dir):
            return
        
        self.target.clean() # Customization point
        shutil.rmtree(self.build_dir, ignore_errors=True)
