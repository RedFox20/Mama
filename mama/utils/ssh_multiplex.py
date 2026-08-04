"""SSH connection multiplexing for git operations: one authenticated master socket per host carries many
parallel git fetches. Adds only options the user has not set, and never touches ssh-agent or the user's keys."""

from __future__ import annotations

import atexit
import contextlib
import os
import re
import shlex
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urlparse

from .system import System


# One master carries every parallel fetch as a separate SSH session, and sshd's default MaxSessions is 10.
# Above that the server refuses the session and the fetch dies. init_fetch_semaphore() clamps to this cap.
DEFAULT_MAX_CONCURRENT_FETCHES = 8

# A UNIX socket path caps at 104 bytes on macOS and 108 on Linux, and ssh expands %C to 40 hex chars.
# A control dir with no room for that fails `unix_listener: path too long`, so the chooser measures each candidate.
_MAX_SOCKET_PATH = 100
_CONTROL_SUBDIR = 'mama-cm'


def _control_dir_candidates() -> list:
    """Where to keep our control sockets, best first. A read-only ~/.ssh loses multiplexing, so a writable
    temp dir comes first, and the uid keeps two users on one shared /tmp apart.
    The joins stay f-strings: normalized_join() calls abspath, which rewrites '/tmp' into 'C:/tmp' on
    Windows, and mama.util costs about 200ms to import on every git ssh spawn."""
    uid = getattr(os, 'geteuid', lambda: 0)()  # Windows has no geteuid, and never opens a master anyway
    runtime = os.environ.get('XDG_RUNTIME_DIR')
    dirs = [f'{runtime.rstrip("/")}/{_CONTROL_SUBDIR}'] if runtime else []
    dirs.append(f'{tempfile.gettempdir().rstrip("/")}/{_CONTROL_SUBDIR}-{uid}')
    dirs.append(f'/tmp/{_CONTROL_SUBDIR}-{uid}')  # macOS $TMPDIR is long enough to exceed the socket budget
    dirs.append(os.path.expanduser('~/.ssh/cm'))  # where mama kept them before, for a host with no temp
    fits = [d for d in dirs if len(d) + 41 <= _MAX_SOCKET_PATH]
    return fits or dirs[-1:]  # every candidate is too long: try the old spot and let ssh report it


_OUR_CONTROL_DIR = _control_dir_candidates()[0]
_OUR_CONTROL_PATH = f'{_OUR_CONTROL_DIR}/%C'

_DEFAULT_KEEPALIVE_INTERVAL = '60'
_DEFAULT_KEEPALIVE_COUNT    = '3'
_DEFAULT_CONTROL_PERSIST    = '10m'

# Always-on so a parallel clone never freezes on a prompt: bound the TCP connect, auto-accept NEW host keys (reject CHANGED).
# NOT BatchMode, which breaks interactive-passphrase keys. The SubProcess idle-timeout backstops a stuck auth prompt.
_SAFETY_OPTS = ['-oConnectTimeout=30', '-oStrictHostKeyChecking=accept-new']


# Module state -------------------------------------------------------------

_state_lock = threading.Lock()
_per_host_locks: dict[tuple, threading.Lock] = {}
_warmed: dict[tuple, dict] = {}     # (user, host, port) -> info
_fetch_semaphore: threading.Semaphore | None = None


# URL parsing --------------------------------------------------------------

# scp-style git URL: [user@]host:path. The regex anchors on a colon not followed by //, which would be ssh://.
_SCP_RE = re.compile(r'^(?:(?P<user>[^@/\s]+)@)?(?P<host>[^:/\s]+):(?!//)')


def parse_ssh_endpoint(url: str) -> tuple[str, str, str | None] | None:
    """Return (user, host, port_or_None) for an SSH git URL: scp-style `git@host:path`, or `ssh://user@host:port/path`.
    Returns None for a https/file URL, a local or Windows drive path, or a colon with no path after it.
    url: the git remote url"""
    if not url:
        return None
    if url.startswith('ssh://'):
        try:
            p = urlparse(url)
        except ValueError:
            return None
        if not p.hostname:
            return None
        port = str(p.port) if p.port else None
        return (p.username or 'git', p.hostname, port)
    if '://' in url:
        return None
    # a drive letter + ':' + slash is a Windows absolute path, which git itself does not treat as an scp URL
    if len(url) >= 3 and url[1] == ':' and url[0].isalpha() and url[2] in ('/', '\\'):
        return None
    m = _SCP_RE.match(url)
    if not m or m.end() >= len(url):
        return None
    host = m.group('host')
    # Bracketed IPv6 in scp-form (`git@[::1]:repo`) is not supported by git.
    if '[' in host or ']' in host:
        return None
    return (m.group('user') or 'git', host, None)


# ssh -G probe -------------------------------------------------------------

def probe_ssh_config(ssh_args: list[str], timeout: float = 5.0) -> dict[str, str]:
    """Run `ssh -G <ssh_args>` and return the effective config (lower-cased keys). Empty dict on failure:
    the probe must never block the build.
    ssh_args: whatever ssh takes after `-G`, typically `[f'{user}@{host}']`, optionally with `-p PORT`
    timeout: seconds before the probe gives up
    Raw subprocess.run with capture_output, not SubProcess.run: every ssh helper in this module must stay
    silent. ssh warns per bad line in the user's ssh_config, and an echoed probe would print that block
    once per dependency. It also runs from mama_ssh.py, a standalone wrapper process with no display to
    route output to. The same applies to every other ssh call below."""
    try:
        cp = subprocess.run(['ssh', '-G', *ssh_args],
                            capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return {}
    if cp.returncode != 0:
        return {}
    out: dict[str, str] = {}
    for line in cp.stdout.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        parts = line.split(None, 1)
        if len(parts) == 2:
            # `ssh -G` prints each key only once, keep the first
            out.setdefault(parts[0].lower(), parts[1])
    return out


def is_multiplex_configured(probe: dict[str, str]) -> bool:
    """User has both ControlMaster (yes/auto/ask/autoask) AND a ControlPath."""
    cm = probe.get('controlmaster', 'no').lower()
    cp = probe.get('controlpath', 'none').lower()
    return cm not in ('no', 'false', '') and cp not in ('none', '', 'no')


def multiplex_known_broken() -> bool:
    """Native Windows: skip multiplex entirely. Microsoft OpenSSH's ControlMaster resets connections
    mid-fetch and leaves stale sockets that disable multiplexing. WSL/Cygwin/Git-Bash report as Linux
    (`System.windows == False`) and keep multiplex."""
    return System.windows


def _control_dir_usable() -> bool:
    """Make our control dir, and report False instead of raising: multiplexing is an optimization and must
    never fail a build. Walks the candidates, so a read-only ~/.ssh costs one fallback, not the whole
    optimization. Without a dir the multiplex flags are skipped and every fetch opens its own connection."""
    global _OUR_CONTROL_DIR, _OUR_CONTROL_PATH
    for candidate in [_OUR_CONTROL_DIR] + _control_dir_candidates():
        try: os.makedirs(candidate, mode=0o700, exist_ok=True)
        except OSError: continue
        if candidate != _OUR_CONTROL_DIR:
            _OUR_CONTROL_DIR = candidate
            _OUR_CONTROL_PATH = f'{candidate}/%C'
        return True
    return False


def options_to_add(probe: dict[str, str]) -> tuple[list[str], bool]:
    """Return (-o args, we_own_master). `we_own_master` is True when mama configures multiplex itself and
    so owns the pre-warm and the cleanup. False when the user already has multiplex, or it is known-broken.
    probe: the effective ssh config from probe_ssh_config()"""
    opts: list[str] = list(_SAFETY_OPTS)
    we_own_master = False
    if not multiplex_known_broken() and not is_multiplex_configured(probe) and _control_dir_usable():
        we_own_master = True
        opts += [
            '-oControlMaster=auto',
            f'-oControlPath={_OUR_CONTROL_PATH}',
            f'-oControlPersist={_DEFAULT_CONTROL_PERSIST}',
        ]
    if probe.get('serveraliveinterval', '0') in ('0', '', None):
        opts += [
            f'-oServerAliveInterval={_DEFAULT_KEEPALIVE_INTERVAL}',
            f'-oServerAliveCountMax={_DEFAULT_KEEPALIVE_COUNT}',
        ]
    return opts, we_own_master


# Per-host setup -----------------------------------------------------------

def _host_lock(key: tuple) -> threading.Lock:
    with _state_lock:
        lk = _per_host_locks.get(key)
        if lk is None:
            lk = threading.Lock()
            _per_host_locks[key] = lk
        return lk


def _probe_args(user: str, host: str, port: str | None) -> list[str]:
    return (['-p', port] if port else []) + [f'{user}@{host}']


def ensure_master_for_url(url: str) -> None:
    """Idempotent, thread-safe. When the user has no multiplex of their own, opens a master connection,
    remembers it for cleanup, and sets GIT_SSH_COMMAND so later git ops use the mama_ssh.py wrapper.
    Blocks the FIRST caller per host while the master opens, later callers return immediately.
    url: the git remote url, a non-SSH url is ignored"""
    ep = parse_ssh_endpoint(url)
    if ep is None:
        return
    if ep in _warmed:
        return

    with _host_lock(ep):
        if ep in _warmed:
            return

        user, host, port = ep
        probe = probe_ssh_config(_probe_args(user, host, port))
        opts, we_own_master = options_to_add(probe)

        if we_own_master:
            state = _open_master(user, host, port, opts)
            # ADOPTED: another run's master was already listening. Use it, but never `ssh -O exit` it at
            # exit: closing another job's connection kills its fetches mid-transfer.
            if state == _MASTER_ADOPTED:
                we_own_master = False
            elif state == _MASTER_FAILED:
                # Pre-warm failed. With the multiplex flags left in opts, every later fetch would race to
                # BECOME the master and trigger N concurrent auths instead of one, the exact thing
                # multiplexing prevents. Strip the flags so each fetch makes its own simple connection.
                opts = [o for o in opts
                        if not (o.startswith('-oControlMaster=')
                                or o.startswith('-oControlPath=')
                                or o.startswith('-oControlPersist='))]
                we_own_master = False

        with _state_lock:
            _warmed[ep] = {'opts': opts, 'we_own_master': we_own_master}

        # only install the wrapper when it has something to do, otherwise it costs a fork+exec per git op
        if opts:
            _set_git_ssh_command()


def _master_control_args(opts: list[str]) -> list[str]:
    """The subset of options needed to address a master on the same socket."""
    return [o for o in opts
            if o.startswith('-oControlPath=') or o.startswith('-oControlPersist=')]


_MASTER_ADOPTED, _MASTER_STARTED, _MASTER_FAILED = 'adopted', 'started', 'failed'


def _master_alive(user: str, host: str, port: str | None, opts: list[str]) -> bool:
    """True when a master already answers on this ControlPath."""
    cmd = ['ssh', '-Ocheck'] + _master_control_args(opts)
    if port:
        cmd += ['-p', port]
    cmd += [f'{user}@{host}']
    try:
        return subprocess.run(cmd, timeout=2, capture_output=True).returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def _remove_stale_socket(user: str, host: str, port: str | None, opts: list[str]) -> None:
    """ssh refuses to open a master while the socket file exists, even dead, and connects unmultiplexed.
    Delete the dead socket, but ONLY under mama's own control dir: a user-configured ControlPath is
    theirs to manage. `ssh -G` expands the %C token."""
    probe = probe_ssh_config(_master_control_args(opts) + _probe_args(user, host, port))
    path = probe.get('controlpath', '')
    if not path.startswith(_OUR_CONTROL_DIR):
        return
    with contextlib.suppress(OSError):
        os.remove(path)


def _open_master(user: str, host: str, port: str | None, opts: list[str]) -> str:
    """Make a master usable on this ControlPath. Returns _MASTER_ADOPTED (one already listened, not mama's
    to close), _MASTER_STARTED (mama owns its cleanup), or _MASTER_FAILED (the caller must drop the
    multiplex flags)."""
    if _master_alive(user, host, port, opts):
        return _MASTER_ADOPTED
    _remove_stale_socket(user, host, port, opts)
    # force ControlMaster=yes for the master itself, replacing any =auto
    cmd = ['ssh', '-fN'] + [o for o in opts if not o.startswith('-oControlMaster=')]
    cmd += ['-oControlMaster=yes']
    if port:
        cmd += ['-p', port]
    cmd += [f'{user}@{host}']
    try:
        # 30s is generous for password/2FA prompts. -fN backgrounds AFTER auth but BEFORE the socket binds, so a poll follows.
        cp = subprocess.run(cmd, timeout=30, capture_output=True, text=True)
        if cp.returncode != 0:
            return _MASTER_FAILED
    except (subprocess.TimeoutExpired, OSError):
        return _MASTER_FAILED
    return _MASTER_STARTED if _wait_master_ready(user, host, port, opts) else _MASTER_FAILED


def _wait_master_ready(user: str, host: str, port: str | None,
                       opts: list[str], deadline_s: float = 5.0) -> bool:
    """Poll `ssh -O check` until the master responds or the deadline passes. `ssh -fN` returns before the
    ControlPath socket binds, and without this poll the first racing fetches each open their own connection."""
    end = time.monotonic() + deadline_s
    delay = 0.05
    while time.monotonic() < end:
        if _master_alive(user, host, port, opts):
            return True
        time.sleep(delay)
        delay = min(delay * 2, 0.5)
    return False


def cleanup_masters() -> None:
    """Run `ssh -O exit` for the masters mama started. Never touch a user-owned one."""
    with _state_lock:
        snapshot = list(_warmed.items())
    for (user, host, port), info in snapshot:
        if not info['we_own_master']:
            continue
        cmd = ['ssh', '-Oexit'] + _master_control_args(info['opts'])
        if port:
            cmd += ['-p', port]
        cmd += [f'{user}@{host}']
        try:
            subprocess.run(cmd, timeout=5, capture_output=True)
        except Exception:
            pass


atexit.register(cleanup_masters)


def _set_git_ssh_command() -> None:
    # an already-set GIT_SSH_COMMAND stays untouched: a user choice, or the wrapper is already installed
    if os.environ.get('GIT_SSH_COMMAND'):
        return
    wrapper = os.path.join(os.path.dirname(__file__), 'mama_ssh.py')
    os.environ['GIT_SSH_COMMAND'] = (
        shlex.quote(sys.executable) + ' ' + shlex.quote(wrapper)
    )


# Concurrent-fetch semaphore -----------------------------------------------

def init_fetch_semaphore(max_concurrent: int = DEFAULT_MAX_CONCURRENT_FETCHES) -> None:
    """Initialize the global semaphore that caps concurrent git fetches, clamped to DEFAULT_MAX_CONCURRENT_FETCHES.
    max_concurrent: the requested cap. `parallel_max` also sizes the scheduler's LOAD pool, where
    artifactory downloads want a high number, so the clamp stays independent of it."""
    global _fetch_semaphore
    n = max(1, min(int(max_concurrent), DEFAULT_MAX_CONCURRENT_FETCHES))
    with _state_lock:
        if _fetch_semaphore is None:
            _fetch_semaphore = threading.Semaphore(n)


def fetch_slot():
    """Context manager that holds a slot in the fetch semaphore. No-op when `init_fetch_semaphore` has not run."""
    return _fetch_semaphore or contextlib.nullcontext()


# Connection pacing --------------------------------------------------------

# Pacing is REACTIVE, off until a host pushes back: a git host can answer a wave of parallel handshakes
# by closing connections mid-handshake, and multiplexing does not cover https remotes or Windows.
# An unconditional stagger would cost wall clock on every run, so the first dropped connection turns it on.
THROTTLED_CONNECT_INTERVAL = 0.25  # seconds between the START of two git network commands, once throttled
_connect_lock = threading.Lock()
_connect_interval = 0.0
_last_connect = 0.0


def note_connection_throttled() -> None:
    """A git command died on a dropped or refused connection: pace every later one for this run."""
    global _connect_interval
    with _connect_lock:
        _connect_interval = THROTTLED_CONNECT_INTERVAL


def pace_new_connection() -> None:
    """Hold a git network command back until the pacing interval has passed since the last one started.
    No-op until note_connection_throttled() fires. Only the START staggers, transfers stay fully parallel."""
    global _last_connect
    with _connect_lock:  # held across the sleep, so N waiters stagger instead of waking together
        if not _connect_interval: return
        # capped at the interval itself: a clock that jumps backwards must never stall a build
        wait = min(_last_connect + _connect_interval - time.monotonic(), _connect_interval)
        if wait > 0: time.sleep(wait)
        _last_connect = time.monotonic()
