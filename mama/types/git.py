import os, shutil, stat, string
from .dep_source import DepSource
from ..utils.system import System, console
from ..utils.sub_process import execute, execute_piped
from ..util import is_dir_empty, write_text_to, read_lines_from

class Git(DepSource):
    """
    For BuildDependency whose source is from a Git repository
    """
    def __init__(self, name:str, url:str, branch:str, tag:str, mamafile:str, args:list):
        super(Git, self).__init__(name)
        if not url: raise RuntimeError("Git url must not be empty!")
        self.is_git = True
        self.url = url
        self.branch = branch
        self.tag = tag
        self.mamafile = mamafile
        self.args = args

        self.from_source = False  # if True, this must be built from source, not from artifactory

        self.missing_status = False
        self.url_changed = False
        self.tag_changed = False
        self.branch_changed = False
        self.commit_changed = False

    def __repr__(self): return self.__str__()
    def __str__(self):
        s = f'DepSource Git {self.name} {self.url}'
        tag = self.branch_or_tag()
        if tag: s += ' ' + tag
        if self.mamafile: s += ' ' + self.mamafile
        return s

    @staticmethod
    def from_papa_string(s: str) -> "Git":
        p = s.split(',')
        name, url, branch, tag, mamafile = p[0:5]
        args = p[5:]
        return Git(name, url, branch, tag, mamafile, args)


    def get_papa_string(self):
        fields = DepSource.papa_join(
            self.name, self.url, self.branch, self.tag, self.mamafile, self.args)
        return 'git ' + fields


    def run_git(self, dep, git_command):
        cmd = f"cd {dep.src_dir} && git {git_command}"
        if dep.config.verbose:
            console(f'  {dep.name: <16} git {git_command}')
        execute(cmd)


    def ls_remote_branch_commit(self, dep):
        """
        Gets the latest remote branch commit, based on git source tag and branch options.
        """
        if self.branch:
            result = execute_piped(f'git ls-remote {self.url} {self.branch}')
        elif self.tag:
            if all(c in string.hexdigits for c in self.tag): # actually commit hash
                result = self.tag
            else:
                result = execute_piped(f'git ls-remote {self.url} {self.tag}')
        else:
            result = execute_piped(f'git ls-remote {self.url} HEAD')

        if result: result = result.split(' ')[0][0:7]
        if dep.config.verbose:
            console(f'git ls-remote {self.url}: {result}')
        return result

    def fetch_origin(self, dep):
        self.run_git(dep, f"pull origin {self.branch_or_tag()} -q")


    def current_commit(self, dep):
        result = execute_piped(['git', 'show', '--oneline', '-s'], cwd=dep.src_dir)
        if dep.config.verbose:
            console(f'  {dep.name: <16} git show --oneline -s:   {result}')
        return result


    def save_status(self, dep):
        status = f"{self.url}\n{self.tag}\n{self.branch}\n{self.current_commit(dep)}\n"
        write_text_to(f"{dep.build_dir}/git_status", status)


    def read_stored_status(self, dep):
        lines = read_lines_from(f"{dep.build_dir}/git_status")
        if not lines: return None
        url = lines[0].rstrip()
        tag = lines[1].rstrip()
        branch = lines[2].rstrip()
        commit = lines[3].rstrip()
        return (url, tag, branch, commit)


    def check_status(self, dep):
        status = self.read_stored_status(dep)
        if not status:
            self.missing_status = True
            if not self.url: return False
            #console(f'check_status {self.url}: NO STATUS AT {dep.build_dir}/git_status')
            self.url_changed = True
            self.tag_changed = True
            self.branch_changed = True
            self.commit_changed = True
            return True
        self.fetch_origin(dep)
        self.url_changed = self.url != status[0]
        self.tag_changed = self.tag != status[1]
        self.branch_changed = self.branch != status[2]
        self.commit_changed = self.current_commit(dep) != status[3]
        #console(f'check_status {self.url} {self.branch_or_tag()}: urlc={self.url_changed} tagc={self.tag_changed} brnc={self.branch_changed} cmtc={self.commit_changed}')
        return self.url_changed or self.tag_changed or self.branch_changed or self.commit_changed


    def branch_or_tag(self):
        if self.branch: return self.branch
        if self.tag: return self.tag
        return ''


    def checkout_current_branch(self, dep):
        branch = self.branch_or_tag()
        if branch:
            if self.tag and self.tag_changed:
                self.run_git(dep, "reset --hard")
            self.run_git(dep, f"checkout {branch}")


    def reclone_wipe(self, dep):
        if dep.config.print:
            console(f'  - Target {dep.name: <16}   RECLONE WIPE')
        if os.path.exists(dep.dep_dir):
            if System.windows: # chmod everything to user so we can delete:
                for root, dirs, files in os.walk(dep.dep_dir):
                    for d in dirs:  os.chmod(os.path.join(root, d), stat.S_IWUSR)
                    for f in files: os.chmod(os.path.join(root, f), stat.S_IWUSR)
            shutil.rmtree(dep.dep_dir)


    def clone_or_pull(self, dep, wiped=False):
        if is_dir_empty(dep.src_dir):
            if not wiped and dep.config.print:
                console(f"  - Target {dep.name: <16}   CLONE because src is missing")
            branch = self.branch_or_tag()
            if branch: branch = f" --branch {self.branch_or_tag()}"
            execute(f"git clone --recurse-submodules --depth 1 {branch} {self.url} {dep.src_dir}", dep.config.verbose)
            self.checkout_current_branch(dep)
        else:
            if dep.config.print:
                console(f"  - Pulling {dep.name: <16}  SCM change detected")
            self.checkout_current_branch(dep)
            execute("git submodule update --init --recursive")
            if not self.tag: # pull if not a tag
                self.run_git(dep, "reset --hard -q")
                self.run_git(dep, "pull")


    def dependency_checkout(self, dep):
        """
        Do a git repository checkout. Can be an expensive operation.
        If an existing artifactory package exists, then this step is skipped
        """
        if not dep.source_dir_exists():  # we MUST pull here
            self.clone_or_pull(dep)
            return True

        config = dep.config
        changed = self.check_status(dep) if config.update else False
        is_target = config.target_matches(self.name)

        wiped = False
        should_wipe = self.url_changed and not self.missing_status
        if should_wipe or (is_target and config.reclone):
            self.reclone_wipe(dep)
            wiped = True
        else:
            # don't pull if no changes to git status
            # or if we're current target of a non-update build
            # mama update target=ReCpp  -- this should git pull
            # mama build target=ReCpp   -- should NOT pull
            non_update_target = is_target and not config.update
            if non_update_target or not changed:
                return False

        self.clone_or_pull(dep, wiped)
        return True

