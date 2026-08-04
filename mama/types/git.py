from __future__ import annotations
from typing import TYPE_CHECKING

import os, string, time, re, random, tempfile, subprocess
from collections import deque
from .dep_source import DepSource
from ..utils.system import Color, console, error, warning, progress
from ..utils.sub_process import SubProcess, execute_piped, execute_piped_echo
from ..utils import ssh_multiplex
from ..util import (is_dir_empty, has_source_content, save_file_if_contents_changed, read_lines_from, path_join,
                    is_network_error, get_time_str, normalized_path, git_dir_fingerprint, git_progress_status,
                    remove_tree, GitError)
from .git_errors import classify_git_failure, format_git_failure, stall_message
from ..mamafile_version import trusted_version


if TYPE_CHECKING:
    from ..build_target import BuildTarget
    from ..build_config import BuildConfig
    from ..build_dependency import BuildDependency


# scp-like ssh syntax `user@host:path/to/repo.git` (no scheme, ':' splits host from path)
_SCP_GIT_RE = re.compile(r'^(?P<user>[^@/]+)@(?P<host>[^:/]+):(?P<path>.+)$')
_SCHEME_RE = re.compile(r'^(?P<scheme>[a-zA-Z][\w+.-]*)://(?P<rest>.*)$')
_FILTERED_GIT_PROGRESS_REPORT_INTERVAL = 0.005

# Benign post-op chatter from git reset/checkout/pull - pure noise outside verbose mode. The
# "merge with the ref ... no such ref was fetched" pair is the expected pull-then-fetch fallback.
_GIT_NOISE = ('Reset branch ', 'Your branch is up to date with ', 'Already up to date',
              'Already on ', 'Switched to branch ', 'Switched to a new branch ', 'HEAD is now at ',
              'Your configuration specifies to merge with the ref ', 'from the remote, but no such ref was fetched',
              'There is no tracking information for the current branch')

# An ssh client built without GSSAPI warns once per fetch about the `GSSAPIAuthentication` line many
# distros ship in /etc/ssh/ssh_config. Auth still works, so the line says nothing about the build.
_SSH_CONFIG_WARNING = re.compile(r'^\S*(?:ssh_config|/config) line \d+: ')

def _is_git_status_noise(line: str) -> bool:
    return line.startswith(_GIT_NOISE) or 'set up to track ' in line \
        or _SSH_CONFIG_WARNING.match(line) is not None


_CLONE_ATTEMPTS = 3
_CLONE_RETRY_BASE = 0.5  # seconds, doubled per attempt and jittered so a throttled wave does not retry in lockstep

# git subcommands that open a connection. The rest are local and must not pay the pacing delay.
_NETWORK_GIT_CMDS = ('fetch', 'pull', 'push', 'clone', 'ls-remote', 'submodule')

_ERROR_TAIL = 40  # git lines kept for the failure report. A fetch's output is otherwise unbounded


def _filter_git_progress(dep, line: str, state: dict, label='') -> bool:
    """True when `line` is git transfer progress, which the caller drops. Collapses the per-percent
    flood into one throttled redraw. EVERY git runner routes output through this single chokepoint."""
    st = git_progress_status(line)
    if st is None: return False
    if dep.config.print:
        now = time.monotonic()
        if 'at' not in state: state['at'] = now  # seed on the first line. No emit until the throttle elapses.
        if st != state.get('last') and (st[1] >= 100 or now - state['at'] >= _FILTERED_GIT_PROGRESS_REPORT_INTERVAL):
            state['last'] = st; state['at'] = now
            tag = f'{label} ' if label else ''
            progress(f'  {dep.name: <16} {tag}{st[0]} {st[1]:3}%')
    return True

def parse_git_url(url: str):
    """Split a remote git url into (scheme, user, host, path). Returns None for
    local paths or anything without a network host, which overrides leave untouched."""
    m = _SCHEME_RE.match(url)
    if m:
        scheme = m.group('scheme').lower()
        if scheme == 'file': return None
        userhost, _, path = m.group('rest').partition('/')
        user, _, hostport = userhost.rpartition('@')
        host = hostport.split(':', 1)[0]  # drop any :port
        return (scheme, user, host, path) if host else None
    m = _SCP_GIT_RE.match(url)
    if m: return ('ssh', m.group('user'), m.group('host'), m.group('path'))
    return None  # local filesystem path

def convert_git_url(url: str, target: str) -> str:
    """Rewrite a git url to 'https' or 'ssh'. Same-protocol urls and local paths
    return unchanged. SSH custom-ports and embedded https credentials are dropped."""
    p = parse_git_url(url)
    if not p: return url
    scheme, _, host, path = p
    path = path.lstrip('/')
    if target == 'https':
        return url if scheme in ('http', 'https') else f'https://{host}/{path}'
    return url if scheme == 'ssh' else f'git@{host}:{path}'

def same_git_remote(a: str, b: str) -> bool:
    """True if two urls point at the same repo ignoring protocol, credentials,
    trailing slashes and a .git suffix - so an ssh<->https override is not a url change."""
    return _canonical_remote(a) == _canonical_remote(b)

def _canonical_remote(url: str) -> str:
    p = parse_git_url(url)
    if not p: return url.rstrip('/')
    _, _, host, path = p
    return f'{host}/{path.strip("/").removesuffix(".git")}'.lower()


class Git(DepSource):
    """For a BuildDependency whose source is a git repository."""
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

        self.from_source = False  # True forces a source build instead of an artifactory package
        self.commit_hash = None  # the git commit hash of this DepSource
        self.url_overridden = False  # True once apply_url_override rewrote self.url

        self.missing_status = False
        self.url_changed = False
        self.tag_changed = False
        self.branch_changed = False
        self.commit_changed = False


    def apply_url_override(self, config: BuildConfig):
        """Rewrite self.url between ssh<->https per config.git_url_override (the
        `https-override` / `ssh-override` build args). Idempotent. No-op for local
        paths or urls already in the target protocol."""
        if not config.git_url_override: return
        new_url = convert_git_url(self.url, config.git_url_override)
        if new_url != self.url:
            if config.verbose: warning(f'  {self.name: <16} URL override: {self.url} -> {new_url}')
            self.url = new_url
            self.url_overridden = True


    def _sync_remote_url(self, dep: BuildDependency):
        """Point an existing clone's origin at the overridden url so fetch/pull use
        the chosen protocol, not the one baked into .git/config at clone time."""
        if self.url_overridden and dep.is_real_clone():
            self.run_git(dep, f'remote set-url origin {self.url}', throw=False)


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


    def _failure_report(self, dep: BuildDependency, action: str, cmd: str, code: int, elapsed: str, output: str,
                        dest='', attempts=1, cause=None) -> str:
        """The message a failed git command raises: the cause first, then the url, the branch or tag, the
        dir and the command mama ran, then the git lines that name the failure. See types/git_errors.py."""
        fields = {'url': self.url, 'tag' if self.tag else 'branch': self.branch_or_tag(), 'dir': dest or dep.src_dir,
                  'command': cmd, 'exit': f'{code} after {elapsed}' + (f', {attempts} attempts' if attempts > 1 else '')}
        return format_git_failure(f'[{action}]  {dep.name}', fields, output, cause or classify_git_failure(output, code))


    def run_git(self, dep: BuildDependency, git_command, throw=True):
        # A shim has no .git, so git run in src_dir would walk up and hit the wrong repo.
        if dep.is_artifactory_shim():
            msg = f'Target {dep.name} is an artifactory shim; cannot run `git {git_command}`'
            if dep.config.verbose: error(f'  {dep.name: <16} {msg}')
            if throw: raise RuntimeError(msg)
            return 1
        cmd = f"git {git_command}"
        if dep.config.verbose:
            warning(f'  {dep.name: <16} {cmd}')
        ssh_multiplex.ensure_master_for_url(self.url)
        # capture and prefix each line, so parallel updates do not tear and each line names its target
        prog: dict = {}
        tail = deque(maxlen=_ERROR_TAIL)  # the last real lines, which the failure report shows
        def prefixed(p:SubProcess, line:str):
            line = line.rstrip()
            if not line: return
            if _filter_git_progress(dep, line, prog): return  # collapse the transfer-progress flood
            noise = _is_git_status_noise(line)  # benign reset/track/up-to-date text
            if not noise: tail.append(line)     # the report shows the same lines as the screen
            if dep.config.verbose or not noise: console(f'  {dep.name: <16} {line}')
        start = time.monotonic()
        with ssh_multiplex.fetch_slot():
            if git_command.split(' ', 1)[0] in _NETWORK_GIT_CMDS: ssh_multiplex.pace_new_connection()
            # cwd= instead of `cd && cmd` because SubProcess uses execve, not a shell.
            # idle_timeout: kill a fetch stuck on an auth prompt so a parallel run never freezes.
            try:
                result = SubProcess.run(cmd, cwd=dep.src_dir, io_func=prefixed, idle_timeout=dep.config.git_timeout)
            except subprocess.TimeoutExpired:
                stalled = stall_message(dep.config.git_timeout)
                tail.append(stalled); error(f'  {dep.name: <16} {stalled}')
                result = -1
        if result != 0 and throw:
            raise GitError(self._failure_report(dep, f'GIT {git_command.split(" ", 1)[0].upper()} FAILED', cmd,
                                                result, get_time_str(time.monotonic() - start), '\n'.join(tail)))
        return result


    def _has_local_modifications(self, dep: BuildDependency) -> bool:
        """True when the working tree has uncommitted modifications to tracked files"""
        return self.run_git(dep, "diff --quiet HEAD", throw=False) != 0


    def _ensure_no_local_modifications(self, dep: BuildDependency):
        """Raise when the working tree has uncommitted changes that an update's reset --hard would
        overwrite. The update path calls this at its TOP, so a dirty dep fails loudly even when
        upstream is unchanged. Otherwise the later pull fails, its fetch fallback swallows the
        error, and the dep silently reports success un-updated."""
        if not self._has_local_modifications(dep): return
        name = dep.name
        error(f"  Target {name} has local modifications that would be overwritten by update.\n"
              f"  To discard local changes and re-fetch, run: `mama wipe {name}`")
        self.run_git(dep, "status --porcelain") # show the modified files
        raise RuntimeError(f"Target {name} has local modifications. Use 'mama wipe {name}' to discard changes.")


    def working_tree_fingerprint(self, dep: BuildDependency) -> str:
        """'' for a clean tree, else a content-aware hash of uncommitted source. See
        util.git_dir_fingerprint. A shim has no working tree on disk, so it counts as clean."""
        return git_dir_fingerprint(dep.src_dir) if dep.is_real_clone() else ''


    def source_tree_changed(self, dep: BuildDependency) -> bool:
        """True when the working-tree source differs from the snapshot stored at the last build."""
        status = self.read_stored_status(dep)
        stored = status[4] if status and len(status) > 4 else ''
        return self.working_tree_fingerprint(dep) != stored


    def get_commit_hash(self, dep: BuildDependency, use_cache=True):
        if not self.commit_hash or not use_cache:
            self.commit_hash = self.init_commit_hash(dep, use_cache=use_cache, fetch_remote=True)
        return self.commit_hash

    @staticmethod
    def get_current_repository_commit(dep: BuildDependency):
        """The short commit hash of the repository at src_dir. The caller makes sure {src_dir}/.git exists."""
        result = execute_piped(['git', 'show', '--format=%h', '-s'], cwd=dep.src_dir)
        if dep.config.verbose:
            console(f'  {dep.name: <16} git show --format=%h -s:   {result}')
        return result

    @staticmethod
    def is_hex_string(s: str) -> bool:
        return len(s) > 0 and all(c in string.hexdigits for c in s)


    def fetch_self_version_from_remote(self, dep: BuildDependency):
        """Fetch only the dep's mamafile, to read `self.version` without the full repo. The shim
        probe uses this for version-pinned deps whose archive name does not track the commit hash.
        The one-shot `git show` uses subprocess.run with stderr=DEVNULL and a timeout, to drop the
        lazy-fetch's `remote: ...` chatter and to bound a stuck fetch. Returns the version or None."""
        if dep.mamafile:
            # A parent-repo mamafile override never resolves through `git show HEAD:<path>` on the
            # remote. mamafile_version.pinned_version already checked the local file for a pin.
            return None
        if not dep.config.is_network_available():
            return None
        mamafile_name = self.mamafile or 'mamafile.py'
        branch = self.branch or self.tag or ''
        branch_arg = f' --branch {branch}' if branch and not Git.is_hex_string(branch) else ''
        try:
            # ignore_cleanup_errors: on Windows git sets read-only on .git/objects/*, which trips
            # shutil.rmtree. normalized_path: shlex.split inside SubProcess eats raw backslash paths.
            with tempfile.TemporaryDirectory(prefix='mama_probe_', ignore_cleanup_errors=True) as tmp:
                tmp = normalized_path(tmp)
                clone_cmd = f'git clone --depth=1 --filter=blob:none --no-checkout{branch_arg} {self.url} {tmp}'
                result, _, elapsed = self._run_git_with_filtered_progress(dep, clone_cmd, label='PROBE')
                if result != 0:
                    if dep.config.print:
                        progress(f'  - Target {dep.name: <16} PROBE FAILED ({result}) after {elapsed}',
                                 color=Color.RED, final=True)
                    return None
                # subprocess.run, not SubProcess.run: see the docstring above
                try:
                    # 10s is enough: the clone already finished, and this fetches a <1KB blob over the same connection.
                    cp = subprocess.run(['git', '-C', tmp, 'show', f'HEAD:{mamafile_name}'],
                                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10)
                except subprocess.TimeoutExpired:
                    if dep.config.verbose: error(f'  {dep.name: <16} PROBE timed out fetching mamafile')
                    return None
                if cp.returncode != 0:
                    return None
                content = cp.stdout.decode('utf-8', errors='replace')
                if not content:
                    return None
                version = trusted_version(dep, content, mamafile_name)
                if dep.config.print and version:
                    progress(f'  - Target {dep.name: <16} PROBE FOUND self.version={version} in {elapsed}',
                             color=Color.BLUE, final=True)
                return version
        except Exception as e:
            if is_network_error(e):
                dep.config.mark_network_unavailable()
            if dep.config.verbose:
                error(f'    {self.name}  sparse-probe failed: {e}')
            return None

    def init_commit_hash(self, dep: BuildDependency, use_cache: bool, fetch_remote: bool):
        """The latest commit hash, based on the git tag and branch options."""
        if not dep.dep_source.is_git:
            return None

        # no update requested: the stored commit hash is enough
        if use_cache and not dep.config.update and os.path.exists(self.git_status_file(dep)):
            status = self.read_stored_status(dep)
            result = status[3].split(' ')[0]
            if dep.config.verbose:
                console(f'    {self.name}  using stored commit hash: {result}')
            return result

        # a hex tag is already a commit pin
        if Git.is_hex_string(self.tag):
            if dep.config.verbose:
                console(f'    {self.name}  using tag as the commit hash: {self.tag}')
            return self.tag

        # an existing repository answers with its current commit
        if os.path.exists(f'{dep.src_dir}/.git'):
            result = Git.get_current_repository_commit(dep)
            if not result:
                error(f'    {self.name}  invalid git repository at {dep.src_dir}')
            return result

        # last resort: ask the remote for the latest commit
        if fetch_remote:
            if not dep.config.is_network_available():
                return None
            arguments = 'HEAD'
            try:
                if self.branch: arguments = self.branch
                elif self.tag:  arguments = self.tag
                ssh_multiplex.ensure_master_for_url(self.url)
                with ssh_multiplex.fetch_slot():
                    ssh_multiplex.pace_new_connection()
                    result = execute_piped(f'git ls-remote {self.url} {arguments}', timeout=5)
                if result: result = result.split(' ')[0][0:7]
                if dep.config.verbose:
                    warning(f'    {self.name}  git ls-remote {self.url} {arguments}: {result}')
                return result
            except Exception as e:
                if is_network_error(e):
                    dep.config.mark_network_unavailable()
                if dep.config.verbose:
                    error(f'    {self.name}  git ls-remote {self.url} {arguments} failed: {e}')
                return None


    def _is_repo_broken(self, dep: BuildDependency) -> bool:
        """`.git` present but this dir is not a usable repo OF ITS OWN. A corrupt `.git` resumes
        git's discovery walk UPWARD, so `rev-parse HEAD` can answer with a PARENT repo, and the pull
        path would then `reset --hard` the user's own checkout. --show-toplevel proves the repo is
        this dir's own. A wrong 'broken' only reaches _refuse_destructive_clone, which keeps real source."""
        out = execute_piped(['git', 'rev-parse', '--show-toplevel', '--verify', '-q', 'HEAD'],
                            cwd=dep.src_dir, throw=False)
        lines = out.splitlines() if out else []
        if len(lines) < 2: return True  # no toplevel and/or no HEAD
        return os.path.realpath(lines[0]) != os.path.realpath(dep.src_dir)


    def _refuse_destructive_clone(self, dep: BuildDependency) -> bool:
        """True when src_dir has real source but no usable .git (rsync'd sandbox copy, local dev work).
        A clone over it destroys uncommitted changes, so build as-is. Only `mama wipe` may discard it."""
        if not has_source_content(dep.src_dir): return False
        if dep.is_current_target() and dep.config.reclone: return False
        if dep.config.print:
            warning(f'  - Target {dep.name: <16} SKIP CLONE (source present, but no usable git repo)')
            console(f'    Building the source as-is. To discard it and re-clone: mama wipe {dep.name}')
        return True


    def _is_detached_head(self, dep: BuildDependency) -> bool:
        """True when the repository is in a detached HEAD state"""
        result = execute_piped(['git', 'symbolic-ref', '-q', 'HEAD'], cwd=dep.src_dir, throw=False)
        return not result


    def _is_rebase_in_progress(self, dep: BuildDependency) -> bool:
        """True when the repository has an active rebase"""
        return os.path.exists(f'{dep.src_dir}/.git/rebase-merge') or \
               os.path.exists(f'{dep.src_dir}/.git/rebase-apply')


    def fetch_origin(self, dep: BuildDependency):
        branch = self.branch_or_tag()
        if Git.is_hex_string(branch):
            return # a commit-hash pin needs no fetch
        if not dep.config.is_network_available():
            return
        if self.tag:
            self.run_git(dep, f"fetch origin tag {branch} -q")
        else:
            # a pull is only safe on the same branch and outside a detached HEAD
            can_pull = not (self.tag_changed or self.branch_changed or self._is_detached_head(dep))
            origin_br = f'origin {branch}' if branch else 'origin'
            result = -1
            if can_pull:
                result = self.run_git(dep, f"pull {origin_br} -q", throw=False)
            if result != 0:
                self.run_git(dep, f"fetch {origin_br} -q")


    def git_status_file(self, dep: BuildDependency):
        return path_join(dep.build_dir, 'git_status')

    @staticmethod
    def format_git_status(url: str, tag: str, branch: str, commit: str, tree: str = ''):
        return f"{url}\n{tag}\n{branch}\n{commit}\n{tree}\n"

    def save_status(self, dep: BuildDependency):
        commit = self.get_commit_hash(dep)
        tree = self.working_tree_fingerprint(dep)
        status = self.format_git_status(self.url, self.tag, self.branch, commit, tree)
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
        tree = lines[4].rstrip() if len(lines) > 4 else ''
        return (url, tag, branch, commit, tree)


    def reset_status(self, dep: BuildDependency):
        """Clear the status file."""
        self.missing_status = True
        status_file = self.git_status_file(dep)
        if os.path.exists(status_file):
            os.remove(status_file)


    def check_status(self, dep: BuildDependency):
        status = self.read_stored_status(dep)
        if not status:
            self.missing_status = True
            if not self.url: return False
            self.url_changed = True
            self.tag_changed = True
            self.branch_changed = True
            self.commit_changed = True
            return True
        # set the flags before fetch_origin, which reads tag_changed and branch_changed for its pull decision
        self.url_changed = not same_git_remote(self.url, status[0])
        self.tag_changed = self.tag != status[1]
        self.branch_changed = self.branch != status[2]
        # then compare the commit hash to detect upstream changes
        self.fetch_origin(dep)
        self.commit_changed = self.get_commit_hash(dep, use_cache=False) != status[3]
        return self.url_changed or self.tag_changed or self.branch_changed or self.commit_changed


    def branch_or_tag(self):
        if self.branch: return self.branch
        if self.tag: return self.tag
        return ''


    def checkout_current_branch_or_tag(self, dep: BuildDependency, is_commit_pin=False):
        branch = self.branch_or_tag()
        if branch:
            if self.tag_changed or self.branch_changed:
                if self._is_rebase_in_progress(dep):
                    self.run_git(dep, "rebase --abort", throw=False)
                self.run_git(dep, "reset --hard")
            if is_commit_pin:
                self.run_git(dep, f"fetch --depth 1 origin {branch}")
                self.run_git(dep, f"checkout {branch}")
            elif self.branch:
                # fetch the branch ref explicitly, so checkout -B always has origin/<branch>
                self.run_git(dep, f"fetch origin +refs/heads/{branch}:refs/remotes/origin/{branch}", throw=False)
                self.run_git(dep, f"checkout -B {branch} origin/{branch}")
            else: # tag
                if self.tag_changed:
                    self.run_git(dep, f"fetch origin tag {branch}")
                self.run_git(dep, f"checkout {branch}")


    def reclone_wipe(self, dep: BuildDependency, source_only: bool = False):
        """Drop this dep's tree so it can be cloned fresh.
        source_only: remove ONLY src_dir. Every AUTOMATIC recovery must use it: the `<dep_dir>/<platform>/`
        siblings hold OTHER platforms' packages, shim markers and build output. A concurrent nested
        build may compile against them right now. The whole dep_dir goes only on an explicit `mama wipe`.
        """
        target = dep.src_dir if source_only else dep.dep_dir
        if dep.config.print:
            console(f'  - Target {dep.name: <16} RECLONE WIPE{" (source)" if source_only else ""}')
        remove_tree(target)


    def _run_git_with_filtered_progress(self, dep: BuildDependency, cmd: str, label: str):
        """Run a git command with progress filtered into one redrawn status line. Returns
        (exit_code, captured_output, elapsed_str). Does not raise. The full clone and the
        mamafile probe share this one progress UI."""
        output = []  # list + join, not output += line (O(n^2) over a big checkout's file list)
        start = time.monotonic()
        prog: dict = {}
        def print_output(p:SubProcess, line:str):
            if _filter_git_progress(dep, line, prog, label=label): return  # same chokepoint as run_git
            if 'Cloning into ' in line:
                return
            elif 'Are you sure you want to continue connecting' in line:
                # TODO: maybe auto-add the host key with ssh-keyscan before the clone?
                console(line)
                p.write('yes\n') # answer the host-key prompt so the clone continues
            elif line:
                output.append(line)
                if dep.config.verbose:
                    console(line)

        if dep.config.verbose:
            console(f'  {dep.name: <16} {cmd}')
        ssh_multiplex.ensure_master_for_url(self.url)
        with ssh_multiplex.fetch_slot():
            ssh_multiplex.pace_new_connection()  # no-op unless a host already pushed back this run
            # idle_timeout: kill a clone stuck on a passphrase prompt so a parallel wave never
            # freezes. A real download streams progress, so the timeout never aborts it.
            try:
                result = SubProcess.run(cmd, io_func=print_output, idle_timeout=dep.config.git_timeout)
            except subprocess.TimeoutExpired:
                output.append(stall_message(dep.config.git_timeout))
                result = -1
        return result, '\n'.join(output), get_time_str(time.monotonic() - start)


    def clone_with_filtered_progress(self, dep: BuildDependency, clone_args: str, clone_to_dir: str):
        cmd = f'git clone {clone_args} {clone_to_dir}'
        for attempt in range(1, _CLONE_ATTEMPTS + 1):
            result, output, elapsed = self._run_git_with_filtered_progress(dep, cmd, label='CLONE')
            if result == 0:
                dep.config.update_stats.record_clone()
                if dep.config.print:
                    progress(f'  - Target {dep.name: <16} CLONE SUCCESS {elapsed}', color=Color.BLUE, final=True)
                    if dep.config.verbose and output: console(output, end='')
                return
            cause = classify_git_failure(output, result)
            if attempt < _CLONE_ATTEMPTS and cause.transient:
                self._backoff_before_reclone(dep, clone_to_dir, attempt, elapsed)
                continue
            if dep.config.print:
                progress(f'  - Target {dep.name: <16} CLONE FAILED ({result}) after {elapsed}', color=Color.RED, final=True)
            raise GitError(self._failure_report(dep, 'CLONE FAILED', cmd, result, elapsed, output,
                                                dest=clone_to_dir, attempts=attempt, cause=cause))


    def _backoff_before_reclone(self, dep: BuildDependency, clone_to_dir: str, attempt: int, elapsed: str):
        """Sleep through the throttle window, then remove the partial tree git left behind. A second
        `git clone` into a non-empty directory fails on the directory and hides the real error."""
        ssh_multiplex.note_connection_throttled()  # the host pushed back: stagger every later connection
        delay = _CLONE_RETRY_BASE * (2 ** (attempt - 1)) * random.uniform(0.5, 1.5)
        if dep.config.print:
            warning(f'  - Target {dep.name: <16} CLONE dropped after {elapsed}, retry '
                    f'{attempt + 1}/{_CLONE_ATTEMPTS} in {get_time_str(delay)}')
        remove_tree(clone_to_dir)
        time.sleep(delay)


    def clone_or_pull(self, dep: BuildDependency, wiped=False):
        # shallow clone by default, unless the config or this dep asks for unshallow
        unshallow = dep.config.unshallow or (not self.shallow)
        if is_dir_empty(dep.src_dir):
            if not dep.config.is_network_available():
                raise RuntimeError(f'Target {dep.name} requires network to clone but network is unavailable.' + \
                                   ' Check your connection or use a cached artifactory package.')
            dep.load_action = 'clone'  # actual full repo clone (display label)
            if not wiped and dep.config.print:
                console(f"  - Target {dep.name: <16} CLONE because src is missing", color=Color.BLUE)
            br_or_tag = self.branch_or_tag()
            is_commit_pin = Git.is_hex_string(br_or_tag)
            checkout_branch = '' if is_commit_pin or len(br_or_tag) == 0 else f' --branch {br_or_tag}'
            depth = '' if unshallow else '--depth 1'
            clone_args = f"{depth} {checkout_branch} {self.url}"
            self.clone_with_filtered_progress(dep, clone_args, dep.src_dir)
            self.checkout_current_branch_or_tag(dep, is_commit_pin=is_commit_pin)
            self.update_submodules(dep, shallow=not unshallow)
        else:
            if not dep.config.is_network_available():
                if dep.config.print:
                    warning(f"  - Target {dep.name: <16} SKIP PULL (network unavailable, using cached source)")
                return
            dep.load_action = 'pulling'  # actual git pull/fetch (display label)
            if dep.config.print:
                console(f"  - Pulling {dep.name: <16}  SCM change detected", color=Color.BLUE)
            self._ensure_no_local_modifications(dep)
            if unshallow:
                self.unshallow(dep)
            is_commit_pin = Git.is_hex_string(self.branch_or_tag())
            self.checkout_current_branch_or_tag(dep, is_commit_pin=is_commit_pin)
            self.update_submodules(dep, shallow=not unshallow)
            if not self.tag: # pull if not a tag
                if self.branch:
                    self.run_git(dep, f"fetch origin {self.branch} -q", throw=False)
                    self.run_git(dep, f"reset --hard origin/{self.branch} -q")
                else:
                    self.run_git(dep, "fetch -q", throw=False)
                    self.run_git(dep, "reset --hard @{upstream} -q") # @{upstream}: see git docs on gitrevisions
            dep.config.update_stats.record_pull()


    def update_submodules(self, dep: BuildDependency, shallow=False):
        """Init and update the submodules of `dep`, and do nothing when it declares none.

        The check is worth its line. On Windows a repository with no submodule at all still pays about
        1 second for `git clone --recurse-submodules`, and about 0.9 seconds for a bare `submodule
        update`. Most dependencies have none, so mama paid both for nothing, once per dependency per
        update. `.gitmodules` is a tracked file at the repository root, so a plain clone still brings
        it and this check reads the truth.
        shallow: clone each submodule at depth 1, to match a shallow parent clone"""
        if not os.path.exists(path_join(dep.src_dir, '.gitmodules')): return
        self.run_git(dep, 'submodule update --init --recursive' + (' --depth 1' if shallow else ''))


    def unshallow(self, dep: BuildDependency):
        # Shallowness detection is unreliable, because the repository can be in several states.
        # .git/shallow is the easy case (what `git rev-parse --is-shallow-repository` checks).
        is_shallow = os.path.exists(f'{dep.src_dir}/.git/shallow')
        if not is_shallow:
            _, output = execute_piped_echo(dep.src_dir, 'git config remote.origin.fetch', echo=False)
            if dep.config.verbose:
                warning(f'  {dep.name: <16} remote.origin.fetch: {output.strip()}')
            if not output or not output.startswith('+refs/heads/*'):
                is_shallow = True # likely a shallow clone

        if is_shallow:
            if dep.config.print:
                warning(f'  - Unshallowing {dep.name}')
            self.run_git(dep, 'config remote.origin.fetch "+refs/heads/*:refs/remotes/origin/*"')
            self.run_git(dep, 'remote update')
            # this last step may fail for a semi-shallow repo (complete history, shallow remote refs)
            self.run_git(dep, 'fetch --unshallow', throw=False)


    def dependency_checkout(self, dep: BuildDependency):
        """Do a git repository checkout, which can be expensive. An existing artifactory package skips this step."""
        # No valid working tree: nothing on disk, files without .git, or a corrupt .git. None of these
        # can pull, so wipe the leftovers and clone fresh. Real source (sandbox rsync, local dev work)
        # is never destroyed: build it as-is.
        if not dep.is_real_clone() or self._is_repo_broken(dep):
            if self._refuse_destructive_clone(dep): return False
            # source_only: a broken tree here says nothing about the sibling platforms sharing this dep_dir.
            # An explicit `mama wipe` still means everything, because the user asked for it.
            if dep.source_dir_exists():
                self.reclone_wipe(dep, source_only=not (dep.is_current_target() and dep.config.reclone))
            self.clone_or_pull(dep)
            return True

        self._sync_remote_url(dep)
        is_target = dep.is_current_target()
        config = dep.config
        changed = False

        if config.update and is_target:
            # fail loudly on a dirty tree BEFORE the pull below - see _ensure_no_local_modifications
            self._ensure_no_local_modifications(dep)
            changed = self.check_status(dep)

        wiped = False
        should_wipe = self.url_changed and not self.missing_status
        explicit_wipe = is_target and config.reclone  # `mama wipe <target>`: the user asked for everything
        if should_wipe or explicit_wipe:
            # a url change re-clones the source, but the sibling platform dirs are not ours to delete
            self.reclone_wipe(dep, source_only=not explicit_wipe)
            wiped = True
        elif dep.config.unshallow and is_target:
            pass # unshallow requested: fall through to clone_or_pull
        else:
            # no pull when the git status shows no change, or for the current target of a non-update
            # build: `mama update target=ReCpp` pulls, `mama build target=ReCpp` does not
            non_update_target = is_target and not config.update
            if non_update_target or not changed:
                if config.verbose:
                    warning(f'    {self.name} git no changes detected and update not specified')
                return False

        self.clone_or_pull(dep, wiped)
        return True
