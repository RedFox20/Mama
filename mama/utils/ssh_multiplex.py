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


# One master carries every parallel fetch as a separate SSH session, and sshd's default MaxSessions is
# 10. Above that the server answers `mux_client_request_session: session request failed: Session open
# refused by peer` and the fetch dies. init_fetch_semaphore() clamps every request to this cap.
DEFAULT_MAX_CONCURRENT_FETCHES = 8

# A UNIX socket path caps at 104 bytes on macOS and 108 on Linux, and ssh expands %C to 40 hex chars.
# A control dir that leaves no room for that produces `unix_listener: path too long`, so the chooser
# below measures every candidate against this budget.
_MAX_SOCKET_PATH = 100
_CONTROL_SUBDIR = 'mama-cm'


def _control_dir_candidates() -> list:
    """Where to keep our control sockets, best first. A container can mount ~/.ssh read-only, and mama
    then loses multiplexing, so a writable temp dir comes first. The uid keeps two users on one shared
    /tmp apart, because a dir another user owns is a dir we cannot write.

    The joins stay f-strings. normalized_join() calls abspath, which rewrites the POSIX '/tmp' fallback
    into 'C:/tmp' on Windows. mama.util also costs about 200ms to import, and mama_ssh.py imports this
    module on every git ssh spawn."""
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

# Always-on so a parallel clone never freezes on an SSH prompt: bound the TCP connect + auto-accept
# NEW host keys (still rejects CHANGED). NOT BatchMode, which would break interactive-passphrase keys.
# The SubProcess idle-timeout is the backstop for stuck auth prompts.
_SAFETY_OPTS = ['-oConnectTimeout=30', '-oStrictHostKeyChecking=accept-new']


# Module state -------------------------------------------------------------

_state_lock = threading.Lock()
_per_host_locks: dict[tuple, threading.Lock] = {}
_warmed: dict[tuple, dict] = {}     # (user, host, port) -> info
_fetch_semaphore: threading.Semaphore | None = None


# URL parsing --------------------------------------------------------------

# scp-style git URL: [user@]host:path  (the path must NOT start with //, that
# would be ssh://). The regex anchors on a colon that is not followed by //.
_SCP_RE = re.compile(r'^(?:(?P<user>[^@/\s]+)@)?(?P<host>[^:/\s]+):(?!//)')


def parse_ssh_endpoint(url: str) -> tuple[str, str, str | None] | None:
    """
    Return (user, host, port_or_None) for an SSH-using git URL, or None.

    Accepts:
        git@github.com:user/repo.git           -> ('git', 'github.com', None)
        ssh://git@host:2222/user/repo.git      -> ('git', 'host', '2222')
    Rejects:
        https://github.com/user/repo.git
        /path/to/local/repo
        file:///...
        C:/foo                                 (Windows path, not scp-style)
        host:                                  (no path after the colon)
    """
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
    # Reject Windows-style absolute paths: a single drive letter followed by
    # `:` and then `/` or `\`. Git itself does not treat these as scp URLs.
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
    """
    Run `ssh -G <ssh_args>` and return the effective config (lower-cased keys).
    Empty dict on failure: the probe must never block the build.

    `ssh_args` is whatever ssh takes after `-G` - typically just
    `[f'{user}@{host}']`, optionally with `-p PORT` etc.

    Raw subprocess.run with capture_output, not SubProcess.run: every ssh helper in this module must
    stay silent. ssh writes a warning per bad line in the user's ssh_config, and a probe that echoed
    those would print the same block once per dependency. It also runs from mama_ssh.py, a standalone
    wrapper process with no display to route output to. The same applies to every other ssh call below.
    """
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
            # `ssh -G` prints each key only once, keep the first.
            out.setdefault(parts[0].lower(), parts[1])
    return out


def is_multiplex_configured(probe: dict[str, str]) -> bool:
    """User has both ControlMaster (yes/auto/ask/autoask) AND a ControlPath."""
    cm = probe.get('controlmaster', 'no').lower()
    cp = probe.get('controlpath', 'none').lower()
    return cm not in ('no', 'false', '') and cp not in ('none', '', 'no')


def multiplex_known_broken() -> bool:
    """Native Windows: skip multiplex entirely. Microsoft OpenSSH's ControlMaster is unreliable:
    `mux_client_request_session: read from master failed: Connection reset by peer` mid-fetch, and a
    stale `ControlSocket ... already exists, disabling multiplexing` after a master drops.
    WSL/Cygwin/Git-Bash report as Linux (`System.windows == False`) and keep multiplex."""
    return System.windows


def _control_dir_usable() -> bool:
    """Make our control dir, and report False instead of raising when we cannot. A CI container often
    runs as a uid that does not own $HOME (GitHub Actions: `/github/home/.ssh` gives Errno 13), and
    multiplexing is an optimization - it must never be the thing that fails a build. Without a dir we
    just skip the multiplex flags and every fetch opens its own connection, as it always could.

    Walks the candidates, so a read-only ~/.ssh costs one fallback instead of the whole optimization."""
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
    """
    Return (-o args, we_own_master). `we_own_master` is True when mama configures multiplex itself,
    which makes mama responsible for the pre-warm and the cleanup. False when the user already has
    multiplex configured, or when multiplex is known-broken on this platform.
    """
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
    """
    Idempotent. Probes the host's SSH config. When the user has no multiplex of their own, opens a
    master connection and remembers it for cleanup. Sets GIT_SSH_COMMAND so later git ops use the
    mama_ssh.py wrapper.

    Safe to call concurrently from multiple threads. Blocks the FIRST caller per host while the
    master opens. Later callers return immediately.
    """
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
            # ADOPTED: a master was already listening (a parallel mama run, or an earlier run's
            # ControlPersist master on a runner that keeps $HOME). Use it, but never `ssh -O exit` it at
            # exit - closing another job's connection kills its fetches mid-transfer.
            if state == _MASTER_ADOPTED:
                we_own_master = False
            elif state == _MASTER_FAILED:
                # Pre-warm failed (auth declined, network blip, host key prompt,
                # MFA timeout). With ControlMaster/ControlPath left in opts,
                # every later fetch would race to BECOME the master and trigger
                # N concurrent auths instead of one - the exact thing
                # multiplexing is meant to prevent. Strip the multiplex flags
                # so each fetch makes its own simple connection.
                opts = [o for o in opts
                        if not (o.startswith('-oControlMaster=')
                                or o.startswith('-oControlPath=')
                                or o.startswith('-oControlPersist='))]
                we_own_master = False

        with _state_lock:
            _warmed[ep] = {'opts': opts, 'we_own_master': we_own_master}

        # Only install the wrapper when it has something to do -
        # otherwise it costs a fork+exec per git op for no benefit.
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
    """
    `ssh -fN -oControlMaster=yes` refuses to open a master while the socket file exists, even when
    nothing listens on it: it prints `ControlSocket ... already exists, disabling multiplexing` and
    connects unmultiplexed. A killed master, or a CI runner that keeps $HOME between jobs, leaves
    exactly that. Delete the dead socket, but ONLY under mama's own control dir: a user-configured
    ControlPath is theirs to manage. `ssh -G` expands the %C token.
    """
    probe = probe_ssh_config(_master_control_args(opts) + _probe_args(user, host, port))
    path = probe.get('controlpath', '')
    if not path.startswith(_OUR_CONTROL_DIR):
        return
    with contextlib.suppress(OSError):
        os.remove(path)


def _open_master(user: str, host: str, port: str | None, opts: list[str]) -> str:
    """
    Make a master usable on this ControlPath. Returns one of:
      _MASTER_ADOPTED - one was already listening. Use it, but it is not mama's to close.
      _MASTER_STARTED - mama opened it, so mama owns its cleanup.
      _MASTER_FAILED  - none available. The caller must drop the multiplex flags, so concurrent
                        fetches do not race to be the master and trigger N parallel auths.
    """
    if _master_alive(user, host, port, opts):
        return _MASTER_ADOPTED
    _remove_stale_socket(user, host, port, opts)
    # Force ControlMaster=yes for the master itself, replacing any =auto.
    cmd = ['ssh', '-fN'] + [o for o in opts if not o.startswith('-oControlMaster=')]
    cmd += ['-oControlMaster=yes']
    if port:
        cmd += ['-p', port]
    cmd += [f'{user}@{host}']
    try:
        # 30s is generous for password/2FA prompts. -fN backgrounds AFTER auth
        # but BEFORE the ControlPath socket binds, so a readiness poll follows.
        cp = subprocess.run(cmd, timeout=30, capture_output=True, text=True)
        if cp.returncode != 0:
            return _MASTER_FAILED
    except (subprocess.TimeoutExpired, OSError):
        return _MASTER_FAILED
    return _MASTER_STARTED if _wait_master_ready(user, host, port, opts) else _MASTER_FAILED


def _wait_master_ready(user: str, host: str, port: str | None,
                       opts: list[str], deadline_s: float = 5.0) -> bool:
    """
    Poll `ssh -O check` until the master responds or the deadline passes.
    `ssh -fN` returns as soon as auth+fork happen, but the ControlPath socket
    can take a brief moment to bind. Without this poll the first racing
    fetches see "no socket yet" and each open their own connection.
    """
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
    # An already-set GIT_SSH_COMMAND stays untouched: either the user made an
    # explicit choice, or the wrapper is already installed.
    if os.environ.get('GIT_SSH_COMMAND'):
        return
    wrapper = os.path.join(os.path.dirname(__file__), 'mama_ssh.py')
    os.environ['GIT_SSH_COMMAND'] = (
        shlex.quote(sys.executable) + ' ' + shlex.quote(wrapper)
    )


# Concurrent-fetch semaphore -----------------------------------------------

def init_fetch_semaphore(max_concurrent: int = DEFAULT_MAX_CONCURRENT_FETCHES) -> None:
    """Initialize the global semaphore that caps concurrent git fetches. The cap clamps to
    DEFAULT_MAX_CONCURRENT_FETCHES whatever the caller asks for. `parallel_max` also sizes the
    scheduler's LOAD pool, where artifactory downloads want a high number. The server refuses
    every git session above its MaxSessions on the shared master."""
    global _fetch_semaphore
    n = max(1, min(int(max_concurrent), DEFAULT_MAX_CONCURRENT_FETCHES))
    with _state_lock:
        if _fetch_semaphore is None:
            _fetch_semaphore = threading.Semaphore(n)


def fetch_slot():
    """
    Context manager that holds a slot in the fetch semaphore. No-op if
    `init_fetch_semaphore` has not been called (e.g. for non-parallel runs).
    """
    return _fetch_semaphore or contextlib.nullcontext()


# Connection pacing --------------------------------------------------------

# Pacing is REACTIVE, and off until a host pushes back. A wave of parallel clones opens its TCP and
# auth handshakes inside a few milliseconds, and a git host can answer that by closing the connection
# mid-handshake. Multiplexing removes the handshakes on a shared SSH host, but an https remote still
# connects per clone and Windows has no multiplexing at all. An unconditional stagger would cost wall
# clock on every run to protect the few that need it, so the first dropped connection turns it on
# instead and everything after that arrives spread out.
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
    """Hold a git network command back until the pacing interval has passed since the last one
    started. No-op until note_connection_throttled() fires. Only the START of each connection is
    staggered, so the transfers themselves stay fully parallel."""
    global _last_connect
    with _connect_lock:  # held across the sleep, so N waiters stagger instead of waking together
        if not _connect_interval: return
        # capped at the interval itself: a clock that jumps backwards must never stall a build
        wait = min(_last_connect + _connect_interval - time.monotonic(), _connect_interval)
        if wait > 0: time.sleep(wait)
        _last_connect = time.monotonic()
