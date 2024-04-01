from __future__ import annotations
from typing import TYPE_CHECKING

import os, shutil, stat, string
from .dep_source import DepSource
from ..utils.system import Color, System, console, error
from ..utils.sub_process import SubProcess, execute, execute_piped, execute_piped_echo
from ..util import is_dir_empty, save_file_if_contents_changed, read_lines_from, path_join


if TYPE_CHECKING:
    from ..build_target import BuildTarget
    from ..build_config import BuildConfig
    from ..build_dependency import BuildDependency

class Git(DepSource):
    """
    For BuildDependency whose source is from a Git repository
    """
    def __init__(self, name:str, url:str, branch:str, tag:str, mamafile:str, shallow:bool, args:list):
        super(Git, self).__init__(name)
        if not url: raise RuntimeError("Git url must not be empty!")
        self.is_git = True
        self.url = url
        self.branch = branch
        self.tag = tag
        self.mamafile = mamafile
        self.shallow = shallow
        self.args = args

        self.from_source = False  # if True, this must be built from source, not from artifactory
        self.commit_hash = None  # the git commit hash of this DepSource

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
        shallow = True # shallow is the default
        return Git(name, url, branch, tag, mamafile, shallow, args)


    def get_papa_string(self):
        fields = DepSource.papa_join(
            self.name, self.url, self.branch, self.tag, self.mamafile, self.args)
        return 'git ' + fields


    def run_git(self, dep: BuildDependency, git_command, throw=True):
        cmd = f"cd {dep.src_dir} && git {git_command}"
        if dep.config.verbose:
            console(f'  {dep.name: <16} git {git_command}', color=Color.YELLOW)
        return execute(cmd, throw=throw)


    def get_commit_hash(self, dep: BuildDependency, use_cache=True):
        if not self.commit_hash or not use_cache:
            self.commit_hash = self.init_commit_hash(dep, use_cache=use_cache, fetch_remote=True)
        return self.commit_hash

    @staticmethod
    def get_current_repository_commit(dep: BuildDependency):
        """ Assuming {src_dir}/.git exists, this will get the repository commit short hash """
        result = execute_piped(['git', 'show', '--format=%h', '-s'], cwd=dep.src_dir)
        if dep.config.verbose:
            console(f'  {dep.name: <16} git show --format=%h -s:   {result}')
        return result


    def init_commit_hash(self, dep: BuildDependency, use_cache: bool, fetch_remote: bool):
        """
        Gets the latest commit hash, based on git source tag and branch options.
        """
        if not dep.dep_source.is_git:
            return None

        # update is not specified? then we can try to skip the check
        if use_cache and not dep.config.update and os.path.exists(self.git_status_file(dep)):
            status = self.read_stored_status(dep)
            result = status[3].split(' ')[0]
            if dep.config.verbose:
                console(f'    {self.name}  using stored commit hash: {result}')
            return result

        # is the tag actually a commit hash?
        if self.tag and all(c in string.hexdigits for c in self.tag):
            if dep.config.verbose:
                console(f'    {self.name}  using tag as the commit hash: {self.tag}')
            return self.tag

        # is this a git repository? we can get the current commit from that
        if os.path.exists(f'{dep.src_dir}/.git'):
            result = Git.get_current_repository_commit(dep)
            if not result:
                error(f'    {self.name}  invalid git repository at {dep.src_dir}')
            return result

        # can we fetch the latest commit from remote instead?
        if fetch_remote:
            arguments = 'HEAD'
            try:
                if self.branch: arguments = self.branch
                elif self.tag:  arguments = self.tag
                result = execute_piped(f'git ls-remote {self.url} {arguments}', timeout=5)
                if result: result = result.split(' ')[0][0:7]
                if dep.config.verbose:
                    console(f'    {self.name}  git ls-remote {self.url} {arguments}: {result}', color=Color.YELLOW)
                return result
            except Exception as e:
                if dep.config.verbose:
                    error(f'    {self.name}  git ls-remote {self.url} {arguments} failed: {e}')
                return None


    def fetch_origin(self, dep: BuildDependency):
        self.run_git(dep, f"pull origin {self.branch_or_tag()} -q")


    def git_status_file(self, dep: BuildDependency):
        return path_join(dep.build_dir, 'git_status')


    def save_status(self, dep: BuildDependency):
        commit = self.get_commit_hash(dep)
        status = f"{self.url}\n{self.tag}\n{self.branch}\n{commit}\n"
        if save_file_if_contents_changed(self.git_status_file(dep), status):
            if dep.config.verbose:
                console(f'    {self.name}  write git status commit={commit}')


    def read_stored_status(self, dep: BuildDependency):
        lines = read_lines_from(self.git_status_file(dep))
        if not lines: return None
        url = lines[0].rstrip()
        tag = lines[1].rstrip()
        branch = lines[2].rstrip()
        commit = lines[3].rstrip()
        return (url, tag, branch, commit)


    def reset_status(self, dep: BuildDependency):
        """ Clears the status file """
        self.missing_status = True
        status_file = self.git_status_file(dep)
        if os.path.exists(status_file):
            os.remove(status_file)


    def check_status(self, dep: BuildDependency):
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
        self.commit_changed = self.get_commit_hash(dep, use_cache=False) != status[3]
        #console(f'check_status {self.url} {self.branch_or_tag()}: urlc={self.url_changed} tagc={self.tag_changed} brnc={self.branch_changed} cmtc={self.commit_changed}')
        return self.url_changed or self.tag_changed or self.branch_changed or self.commit_changed


    def branch_or_tag(self):
        if self.branch: return self.branch
        if self.tag: return self.tag
        return ''


    def checkout_current_branch(self, dep: BuildDependency):
        branch = self.branch_or_tag()
        if branch:
            if self.tag and self.tag_changed:
                self.run_git(dep, "reset --hard")
            self.run_git(dep, f"checkout {branch}")


    def reclone_wipe(self, dep: BuildDependency):
        if dep.config.print:
            console(f'  - Target {dep.name: <16} RECLONE WIPE')
        if os.path.exists(dep.dep_dir):
            if System.windows: # chmod everything to user so we can delete:
                for root, dirs, files in os.walk(dep.dep_dir):
                    for d in dirs:  os.chmod(os.path.join(root, d), stat.S_IWUSR)
                    for f in files: os.chmod(os.path.join(root, f), stat.S_IWUSR)
            shutil.rmtree(dep.dep_dir)


    def clone_with_filtered_progress(self, dep: BuildDependency, clone_args: str, clone_to_dir: str):
        output = ''
        current_percent = -1
        def print_output(p:SubProcess, line:str):
            nonlocal output, current_percent
            if 'remote: Counting objects:' in line or \
                'remote: Compressing objects:' in line or \
                'Receiving objects:' in line or \
                'Resolving deltas:' in line or \
                'Updating files:' in line:
                if dep.config.print:
                    parts = line.split('%')[0].split(':')
                    if not parts:
                        console(line)
                        return
                    percent_string = parts[len(parts)-1].strip()
                    percent = int(percent_string) if percent_string else 0
                    if current_percent != percent:
                        current_percent = percent
                        status = 'status             '
                        if 'remote: Counting objects:' in line:      status = 'counting objects   '
                        elif 'remote: Compressing objects:' in line: status = 'compressing objects'
                        elif 'Receiving objects:' in line:           status = 'receiving objects  '
                        elif 'Resolving deltas:' in line:            status = 'resolving deltas   '
                        elif 'Updating files:' in line:              status = 'updating files     '
                        console(f'\r  - Target {dep.name: <16} CLONE {status} {current_percent:3}%', end='')
            elif 'Cloning into ' in line:
                return
            elif 'Are you sure you want to continue connecting' in line:
                # TODO: maybe auto-add the key before running clone?
                # if [ ! -n "$(grep "^bitbucket.org " ~/.ssh/known_hosts)" ]; then ssh-keyscan bitbucket.org >> ~/.ssh/known_hosts 2>/dev/null; fi
                console(line)
                p.write('yes\n') # get us unstuck
            elif line:
                output += line
                output += '\n'
                if dep.config.verbose:
                    console(line)

        # run the command, working dir not needed since it should be a full path in the clone_args
        cmd = f'git clone {clone_args} {clone_to_dir}'
        if dep.config.verbose:
            console(f'  {dep.name: <16} {cmd}')
        result = SubProcess.run(cmd, io_func=print_output)

        # handle the result:
        if dep.config.print:
            if result == 0:
                console(f'\r  - Target {dep.name: <16} CLONE SUCCESS                  ', color=Color.BLUE)
                if dep.config.verbose and output:
                    console(output, end='')
            else:
                console(f'\r  - Target {dep.name: <16} CLONE FAILED ({result})              ', color=Color.RED)
                if output:
                    console(output, end='')
                raise RuntimeError(f'Target {self.name} clone failed: {cmd}')


    def clone_or_pull(self, dep: BuildDependency, wiped=False):
        # by default we create a shallow clone, unless unshallow is specified in config or this dep
        unshallow = dep.config.unshallow or (not self.shallow)
        if is_dir_empty(dep.src_dir):
            if not wiped and dep.config.print:
                console(f"  - Target {dep.name: <16} CLONE because src is missing", color=Color.BLUE)
            branch = self.branch_or_tag()
            if branch: branch = f" --branch {self.branch_or_tag()}"
            depth = '' if unshallow else '--depth 1'
            clone_args = f"--recurse-submodules {depth} {branch} {self.url}"
            self.clone_with_filtered_progress(dep, clone_args, dep.src_dir)
            self.checkout_current_branch(dep)
        else:
            if dep.config.print:
                console(f"  - Pulling {dep.name: <16}  SCM change detected", color=Color.BLUE)
            if unshallow:
                self.unshallow(dep)
            self.checkout_current_branch(dep)
            self.run_git(dep, 'submodule update --init --recursive')
            if not self.tag: # pull if not a tag
                self.run_git(dep, "reset --hard -q")
                self.run_git(dep, "pull")


    def unshallow(self, dep: BuildDependency):
        # Detecting shallowness can be quite tricky, since the repository can be in multiple different states
        # Detecting the easy cases first:
        #   git rev-parse --is-shallow-repository --> .git/shallow exists
        is_shallow = os.path.exists(f'{dep.src_dir}/.git/shallow')
        if not is_shallow:
            _, output = execute_piped_echo(dep.src_dir, 'git config remote.origin.fetch', echo=False)
            if dep.config.verbose:
                console(f'  {dep.name: <16} remote.origin.fetch: {output.strip()}', color=Color.YELLOW)
            if not output or not output.startswith('+refs/heads/*'):
                is_shallow = True # likely a shallow clone

        if is_shallow:
            if dep.config.print:
                console(f'  - Unshallowing {dep.name}', color=Color.YELLOW)
            self.run_git(dep, 'config remote.origin.fetch "+refs/heads/*:refs/remotes/origin/*"')
            self.run_git(dep, 'remote update')
            # this last step is allowed to fail, just in case it was 
            # semi-shallow (history was complete, but remote refs were shallow)
            self.run_git(dep, 'fetch --unshallow', throw=False)


    def dependency_checkout(self, dep: BuildDependency):
        """
        Do a git repository checkout. Can be an expensive operation.
        If an existing artifactory package exists, then this step is skipped
        """
        if not dep.source_dir_exists():  # we MUST pull here
            self.clone_or_pull(dep)
            return True

        is_target = dep.is_current_target()
        config = dep.config
        changed = False

        if config.update and is_target:
            changed = self.check_status(dep)

        wiped = False
        should_wipe = self.url_changed and not self.missing_status
        if should_wipe or (is_target and config.reclone):
            self.reclone_wipe(dep)
            wiped = True
        elif dep.config.unshallow and is_target: # unshallow was specified, we should at least pull
            pass # fallthrough to clone_or_pull
        else:
            # don't pull if no changes to git status
            # or if we're current target of a non-update build
            # mama update target=ReCpp  -- this should git pull
            # mama build target=ReCpp   -- should NOT pull
            non_update_target = is_target and not config.update
            if non_update_target or not changed:
                if config.verbose:
                    console(f'    {self.name} git no changes detected and update not specified', color=Color.YELLOW)
                return False

        self.clone_or_pull(dep, wiped)
        return True

