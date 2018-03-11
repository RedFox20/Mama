import os, subprocess, shutil, stat
from .parse_mamafile import parse_mamafile
from .system import System, console, execute
from .util import is_dir_empty, has_tag_changed, write_text_to

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
        self.children   = []
        if not src and not git:
            raise RuntimeError(f'{name} src and git not configured. Specify at least one.')

        if src:
            self.target = self.create_build_target(src)
            self.name = self.target.name

        dep_name = self.name
        if git:
            if git.branch: dep_name = f'{self.name}-{git.branch}'
            elif git.tag:  dep_name = f'{self.name}-{git.tag}'
        self.dep_dir   = os.path.join(config.workspaces_root, self.workspace, dep_name)
        self.build_dir = os.path.join(self.dep_dir, config.name())

        if git:
            self.git     = git
            self.src_dir = os.path.join(self.dep_dir, self.name)
            git.dep      = self
            self.target  = None
        else:
            self.git     = None
            self.src_dir = src

    def is_reconfigure_target(self):
        return self.config.configure and self.config.target == self.name

    ## @return True if dependency has changed
    def load(self):
        git_changed = self.git_checkout()
        target = self.create_build_target()
        target.dependencies()

        conf = self.config
        if conf.clean and (conf.target == 'all' or conf.target == target.name):
            self.clean(conf.target)

        changed = False
        if conf.build:
            if git_changed:
                console(f'  - Target {target.name: <16}   BUILD because git commit changed')
                changed = True
            elif not target.build_dependency:
                console(f'  - Target {target.name: <16}   BUILD because there is no configured build_dependency')
                changed = True
            elif not os.path.exists(target.build_dependency):
                console(f'  - Target {target.name: <16}   BUILD because {target.build_dependency} does not exist')
                changed = True
            elif self.is_reconfigure_target():
                console(f'  - Target {target.name: <16}   BUILD because configure target={target.name}')
                changed = True
            else:
                console(f'  - Target {target.name: <16}   OK')

        if not os.path.exists(self.build_dir):
            os.makedirs(self.build_dir, exist_ok=True)

        self.should_rebuild = changed
        return changed

    def create_build_target(self, src=None):
        if self.target:
            return self.target

        if not src: src = self.src_dir
        project, buildTarget = parse_mamafile(self.config, src, self.target_class)
        buildStatics = buildTarget.__dict__

        if not self.workspace:
            self.workspace = buildStatics['workspace'] if 'workspace' in buildStatics else 'mamabuild'
        
        self.target = buildTarget(name=project, config=self.config, dep=self)
        return self.target


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
    def clean(self, because=None):
        if self.build_dir == '/' or not os.path.exists(self.build_dir):
            return
        if not because: because = self.name
        console(f'  - Target {self.name: <16}   CLEAN because target={because}')
        shutil.rmtree(self.build_dir, ignore_errors=True)
