"""
SSH connection multiplexing for git operations.

When mama runs `update` against many private git repositories on the same SSH
host (e.g. github.com), each `git fetch` opens a fresh SSH connection and pays
the full auth cost. Multiplexing lets a single auth'd master socket carry many
parallel git ops.

Design rules:
* Probe via `ssh -G user@host` to read the user's effective config. We never
  override settings the user has already configured (ControlMaster,
  ControlPath, ServerAliveInterval, ServerAliveCountMax). Our `-o` flags are
  added only for keys the user has not set.
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


DEFAULT_MAX_CONCURRENT_FETCHES = 20

_OUR_CONTROL_DIR = os.path.expanduser('~/.ssh/cm')
_OUR_CONTROL_PATH = os.path.join(_OUR_CONTROL_DIR, '%C')

_DEFAULT_KEEPALIVE_INTERVAL = '60'
_DEFAULT_KEEPALIVE_COUNT    = '3'
_DEFAULT_CONTROL_PERSIST    = '10m'

# Marker so the wrapper knows we set GIT_SSH_COMMAND ourselves and didn't
# inherit it from the user.
_OWNED_ENV = 'MAMA_SSH_MUX_OWNED'


# Module state -------------------------------------------------------------

_state_lock = threading.Lock()
_per_host_locks: dict[str, threading.Lock] = {}
_warmed: dict[str, dict] = {}     # host_key -> info dict
_fetch_semaphore: threading.Semaphore | None = None
_atexit_registered = False
_verbose = False


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
    # Reject anything that has a non-ssh scheme.
    if '://' in url:
        return None
    # Reject Windows-style absolute paths: a single drive letter followed by
    # `:` and then `/` or `\`. Git itself doesn't treat these as scp URLs.
    if len(url) >= 3 and url[1] == ':' and url[0].isalpha() and url[2] in ('/', '\\'):
        return None
    m = _SCP_RE.match(url)
    if not m:
        return None
    host = m.group('host')
    # Require the colon to be followed by a non-empty path component;
    # `host:` with nothing after isn't a real git URL.
    if m.end() >= len(url):
        return None
    # Bracketed IPv6 in scp-form (`git@[::1]:repo`) is not supported by git
    # itself; punt to None rather than report a bogus host.
    if '[' in host or ']' in host:
        return None
    return (m.group('user') or 'git', host, None)


def host_key(user: str, host: str, port: str | None) -> str:
    return f'{user}@{host}:{port or ""}'


# ssh -G probe -------------------------------------------------------------

def probe_ssh_config(user: str, host: str, port: str | None,
                     timeout: float = 5.0) -> dict[str, str]:
    """
    Run `ssh -G [-p port] user@host` and return effective config (lower-cased
    keys). Empty dict on failure — probe must never block the build.
    """
    cmd = ['ssh', '-G']
    if port:
        cmd += ['-p', port]
    cmd += [f'{user}@{host}']
    try:
        cp = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
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
            key = parts[0].lower()
            if key not in out:
                out[key] = parts[1]
    return out


def is_multiplex_configured(probe: dict[str, str]) -> bool:
    """User has both ControlMaster (yes/auto/ask/autoask) AND a ControlPath."""
    cm = probe.get('controlmaster', 'no').lower()
    cp = probe.get('controlpath', 'none').lower()
    return cm not in ('no', 'false', '') and cp not in ('none', '', 'no')


def _is_keepalive_configured(probe: dict[str, str]) -> bool:
    interval = probe.get('serveraliveinterval', '0')
    return interval not in ('0', '', None)


def options_to_add(probe: dict[str, str]) -> tuple[list[str], bool]:
    """
    Return (-o args, we_own_master). `we_own_master` is True when we are the
    one configuring multiplex (and therefore responsible for pre-warming and
    cleaning it up). False if the user already has multiplex configured.
    """
    opts: list[str] = []
    we_own_master = False
    if not is_multiplex_configured(probe):
        we_own_master = True
        os.makedirs(_OUR_CONTROL_DIR, mode=0o700, exist_ok=True)
        opts += [
            '-oControlMaster=auto',
            f'-oControlPath={_OUR_CONTROL_PATH}',
            f'-oControlPersist={_DEFAULT_CONTROL_PERSIST}',
        ]
    if not _is_keepalive_configured(probe):
        opts += [
            f'-oServerAliveInterval={_DEFAULT_KEEPALIVE_INTERVAL}',
            f'-oServerAliveCountMax={_DEFAULT_KEEPALIVE_COUNT}',
        ]
    return opts, we_own_master


# Per-host setup -----------------------------------------------------------

def _host_lock(key: str) -> threading.Lock:
    with _state_lock:
        lk = _per_host_locks.get(key)
        if lk is None:
            lk = threading.Lock()
            _per_host_locks[key] = lk
        return lk


def set_verbose(v: bool) -> None:
    global _verbose
    _verbose = bool(v)


def _log(msg: str) -> None:
    if _verbose:
        sys.stderr.write(f'[ssh-mux] {msg}\n')


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
    user, host, port = ep
    key = host_key(user, host, port)

    if key in _warmed:
        return

    with _host_lock(key):
        if key in _warmed:
            return

        probe = probe_ssh_config(user, host, port)
        opts, we_own_master = options_to_add(probe)

        if we_own_master:
            master_up = _start_master(user, host, port, opts)
            if not master_up:
                # Pre-warm failed (auth declined, network blip, host key
                # prompt, MFA timeout). If we left ControlMaster/ControlPath
                # in opts, every subsequent fetch would race to BECOME the
                # master and we'd trigger N concurrent auths instead of one —
                # the exact thing multiplexing is meant to prevent. Strip
                # the multiplex flags so each fetch makes its own simple
                # connection. Keepalives are still useful and stay.
                opts = [o for o in opts
                        if not (o.startswith('-oControlMaster=')
                                or o.startswith('-oControlPath=')
                                or o.startswith('-oControlPersist='))]
                we_own_master = False

        with _state_lock:
            _warmed[key] = {
                'user': user, 'host': host, 'port': port,
                'opts': opts, 'we_own_master': we_own_master,
            }
        # Only install the GIT_SSH_COMMAND wrapper when there's something for
        # it to do. If the user has every host configured to their liking the
        # wrapper would only add a fork+exec per git op for no benefit.
        if opts:
            _set_git_ssh_command()
        if we_own_master:
            _ensure_atexit()


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
    cmd = ['ssh', '-fN']
    # Force ControlMaster=yes for the master itself; replace any =auto.
    cmd += [o for o in opts if not o.startswith('-oControlMaster=')]
    cmd += ['-oControlMaster=yes']
    if port:
        cmd += ['-p', port]
    cmd += [f'{user}@{host}']
    _log(f'opening master: {" ".join(shlex.quote(c) for c in cmd)}')
    try:
        # 30s is generous for password/2FA prompts. -fN backgrounds AFTER auth
        # but BEFORE the ControlPath socket is bound, so we still need to poll.
        cp = subprocess.run(cmd, timeout=30, capture_output=True, text=True)
        if cp.returncode != 0:
            _log(f'master start rc={cp.returncode} stderr={cp.stderr.strip()}')
            return False
    except subprocess.TimeoutExpired:
        _log(f'master start timed out for {host}')
        return False
    except (OSError, FileNotFoundError) as e:
        _log(f'master start failed for {host}: {e}')
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
            cp = subprocess.run(check, timeout=2, capture_output=True, text=True)
        except (subprocess.TimeoutExpired, OSError):
            return False
        if cp.returncode == 0:
            return True
        _t.sleep(delay)
        delay = min(delay * 2, 0.5)
    _log(f'master never became ready for {host}')
    return False


def cleanup_masters() -> None:
    """Run `ssh -O exit` for masters we started. Don't touch user-owned ones."""
    with _state_lock:
        snapshot = list(_warmed.values())
    for info in snapshot:
        if not info.get('we_own_master'):
            continue
        cmd = ['ssh', '-Oexit'] + _master_control_args(info['opts'])
        if info.get('port'):
            cmd += ['-p', info['port']]
        cmd += [f'{info["user"]}@{info["host"]}']
        try:
            subprocess.run(cmd, timeout=5, capture_output=True)
        except Exception:
            pass


def _ensure_atexit() -> None:
    global _atexit_registered
    if not _atexit_registered:
        atexit.register(cleanup_masters)
        _atexit_registered = True


def _set_git_ssh_command() -> None:
    # If user already set GIT_SSH_COMMAND we leave it alone — they've made an
    # explicit choice. Our wrapper would override that.
    if os.environ.get('GIT_SSH_COMMAND') and os.environ.get(_OWNED_ENV) != '1':
        return
    wrapper = wrapper_script_path()
    os.environ['GIT_SSH_COMMAND'] = (
        shlex.quote(sys.executable) + ' ' + shlex.quote(wrapper)
    )
    os.environ[_OWNED_ENV] = '1'


def wrapper_script_path() -> str:
    return os.path.join(os.path.dirname(__file__), 'mama_ssh.py')


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
