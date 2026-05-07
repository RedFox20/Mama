"""
SSH connection multiplexing for git operations.

When mama runs `update` against many private git repositories on the same SSH
host (e.g. github.com), each `git fetch` opens a fresh SSH connection and pays
the full auth cost. Multiplexing lets a single auth'd master socket carry many
parallel git ops.

Design rules:
* Probe via `ssh -G user@host` to read the user's effective config. We never
  override settings the user already has (ControlMaster, ControlPath,
  ServerAliveInterval, ServerAliveCountMax). Our `-o` flags are added only
  for keys the user has not set.
* `GIT_SSH_COMMAND` points at a small wrapper (`mama_ssh.py`) so per-host
  options can be applied just-in-time. A single global GIT_SSH_COMMAND can't
  encode per-host decisions, so the wrapper does it on each invocation.
* Pre-warm one master per host with `ssh -fN` BEFORE we kick off parallel git
  ops, so 20 concurrent fetches don't trigger 20 concurrent auths.
* Track which masters we started; clean them up at exit. Pre-existing masters
  the user manages are left alone.
* Never touch ssh-agent: no IdentityAgent, no IdentityFile, no manipulation of
  SSH_AUTH_SOCK. The user's keys and agent stay exactly as configured.
"""

from __future__ import annotations

import atexit
import contextlib
import os
import re
import shlex
import subprocess
import sys
import threading
from urllib.parse import urlparse

from .system import System


DEFAULT_MAX_CONCURRENT_FETCHES = 20

_OUR_CONTROL_DIR = os.path.expanduser('~/.ssh/cm')
_OUR_CONTROL_PATH = os.path.join(_OUR_CONTROL_DIR, '%C')

_DEFAULT_KEEPALIVE_INTERVAL = '60'
_DEFAULT_KEEPALIVE_COUNT    = '3'
_DEFAULT_CONTROL_PERSIST    = '10m'


# Module state -------------------------------------------------------------

_state_lock = threading.Lock()
_per_host_locks: dict[tuple, threading.Lock] = {}
_warmed: dict[tuple, dict] = {}     # (user, host, port) -> info
_fetch_semaphore: threading.Semaphore | None = None


# URL parsing --------------------------------------------------------------

# scp-style git URL: [user@]host:path  (the path must NOT start with //, that
# would be ssh://). We anchor on a colon that isn't followed by //.
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
        host:                                  (no path after colon)
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
    # `:` and then `/` or `\`. Git itself doesn't treat these as scp URLs.
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
    Run `ssh -G <ssh_args>` and return effective config (lower-cased keys).
    Empty dict on failure — probe must never block the build.

    `ssh_args` is whatever you'd pass to ssh after `-G` — typically just
    `[f'{user}@{host}']`, optionally with `-p PORT` etc.
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
            # `ssh -G` prints each key only once; keep first.
            out.setdefault(parts[0].lower(), parts[1])
    return out


def is_multiplex_configured(probe: dict[str, str]) -> bool:
    """User has both ControlMaster (yes/auto/ask/autoask) AND a ControlPath."""
    cm = probe.get('controlmaster', 'no').lower()
    cp = probe.get('controlpath', 'none').lower()
    return cm not in ('no', 'false', '') and cp not in ('none', '', 'no')


def multiplex_known_broken() -> bool:
    """Native Windows: skip multiplex entirely. Microsoft OpenSSH's
    ControlMaster is unreliable in practice — `mux_client_request_session:
    read from master failed: Connection reset by peer` mid-fetch and stale
    `ControlSocket ... already exists, disabling multiplexing` after a master
    drops. WSL/Cygwin/Git-Bash run as Linux from Python's POV
    (`System.windows == False`) and keep multiplex."""
    return System.windows


def options_to_add(probe: dict[str, str]) -> tuple[list[str], bool]:
    """
    Return (-o args, we_own_master). `we_own_master` is True when we are the
    one configuring multiplex (and therefore responsible for pre-warming and
    cleaning it up). False if the user already has multiplex configured, or
    if multiplex is known-broken on this platform.
    """
    opts: list[str] = []
    we_own_master = False
    if not multiplex_known_broken() and not is_multiplex_configured(probe):
        we_own_master = True
        os.makedirs(_OUR_CONTROL_DIR, mode=0o700, exist_ok=True)
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
    Idempotent. Probes the host's SSH config and, if multiplexing isn't
    already set up by the user, opens a master connection and remembers it
    for cleanup. Sets GIT_SSH_COMMAND so subsequent git ops use our wrapper.

    Safe to call concurrently from multiple threads. Blocks the FIRST caller
    per host while the master is being established; subsequent callers return
    immediately.
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

        if we_own_master and not _start_master(user, host, port, opts):
            # Pre-warm failed (auth declined, network blip, host key prompt,
            # MFA timeout). If we left ControlMaster/ControlPath in opts,
            # every subsequent fetch would race to BECOME the master and
            # we'd trigger N concurrent auths instead of one — the exact
            # thing multiplexing is meant to prevent. Strip the multiplex
            # flags so each fetch makes its own simple connection.
            opts = [o for o in opts
                    if not (o.startswith('-oControlMaster=')
                            or o.startswith('-oControlPath=')
                            or o.startswith('-oControlPersist='))]
            we_own_master = False

        with _state_lock:
            _warmed[ep] = {'opts': opts, 'we_own_master': we_own_master}

        # Only install the wrapper when there's something for it to do —
        # otherwise it's a fork+exec per git op for no benefit.
        if opts:
            _set_git_ssh_command()


def _master_control_args(opts: list[str]) -> list[str]:
    """The subset of options needed to address a master on the same socket."""
    return [o for o in opts
            if o.startswith('-oControlPath=') or o.startswith('-oControlPersist=')]


def _start_master(user: str, host: str, port: str | None, opts: list[str]) -> bool:
    """
    Open a master in the background with `ssh -fN` and verify it's listening
    via `ssh -O check`. Returns True only if the master is confirmed ready —
    callers should downgrade to non-multiplexed mode on False so concurrent
    fetches don't all race to be the master and trigger N parallel auths.
    """
    # Force ControlMaster=yes for the master itself; replace any =auto.
    cmd = ['ssh', '-fN'] + [o for o in opts if not o.startswith('-oControlMaster=')]
    cmd += ['-oControlMaster=yes']
    if port:
        cmd += ['-p', port]
    cmd += [f'{user}@{host}']
    try:
        # 30s is generous for password/2FA prompts. -fN backgrounds AFTER auth
        # but BEFORE the ControlPath socket is bound, so we still need to poll.
        cp = subprocess.run(cmd, timeout=30, capture_output=True, text=True)
        if cp.returncode != 0:
            return False
    except (subprocess.TimeoutExpired, OSError):
        return False
    return _wait_master_ready(user, host, port, opts)


def _wait_master_ready(user: str, host: str, port: str | None,
                       opts: list[str], deadline_s: float = 5.0) -> bool:
    """
    Poll `ssh -O check` until the master responds or we hit the deadline.
    `ssh -fN` returns as soon as auth+fork happen, but the ControlPath socket
    can take a brief moment to bind. Without this poll the first racing
    fetches see "no socket yet" and each open their own connection.
    """
    import time as _t
    check = ['ssh', '-Ocheck'] + _master_control_args(opts)
    if port:
        check += ['-p', port]
    check += [f'{user}@{host}']
    end = _t.monotonic() + deadline_s
    delay = 0.05
    while _t.monotonic() < end:
        try:
            cp = subprocess.run(check, timeout=2, capture_output=True)
        except (subprocess.TimeoutExpired, OSError):
            return False
        if cp.returncode == 0:
            return True
        _t.sleep(delay)
        delay = min(delay * 2, 0.5)
    return False


def cleanup_masters() -> None:
    """Run `ssh -O exit` for masters we started. Don't touch user-owned ones."""
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
    # If GIT_SSH_COMMAND is already set we leave it alone — either the user
    # made an explicit choice or we already installed our wrapper.
    if os.environ.get('GIT_SSH_COMMAND'):
        return
    wrapper = os.path.join(os.path.dirname(__file__), 'mama_ssh.py')
    os.environ['GIT_SSH_COMMAND'] = (
        shlex.quote(sys.executable) + ' ' + shlex.quote(wrapper)
    )


# Concurrent-fetch semaphore -----------------------------------------------

def init_fetch_semaphore(max_concurrent: int = DEFAULT_MAX_CONCURRENT_FETCHES) -> None:
    """Initialise the global semaphore that caps concurrent git fetches."""
    global _fetch_semaphore
    n = max(1, int(max_concurrent))
    with _state_lock:
        if _fetch_semaphore is None:
            _fetch_semaphore = threading.Semaphore(n)


def fetch_slot():
    """
    Context manager that holds a slot in the fetch semaphore. No-op if
    `init_fetch_semaphore` has not been called (e.g. for non-parallel runs).
    """
    return _fetch_semaphore or contextlib.nullcontext()
