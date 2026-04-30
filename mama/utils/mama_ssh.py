#!/usr/bin/env python3
"""
GIT_SSH_COMMAND wrapper for mama.

Git invokes us as if we were ssh:
    mama_ssh.py [ssh-args] [user@]host command-on-remote

We don't bother parsing ssh's options to find the host — we just hand the
same args (minus the trailing remote command) to `ssh -G`, which gives us
the user's effective config for that destination. Then we decide which
extra `-o` flags to add (multiplexing, keepalives) WITHOUT overriding
anything the user already configured, and exec ssh with the augmented args.

If anything goes wrong we still exec ssh with the original args. Never
break a build because of multiplexing setup.
"""

from __future__ import annotations

import os
import subprocess
import sys

# Allow running as a standalone script, not just as a package module.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if __package__ in (None, ''):
    sys.path.insert(0, os.path.dirname(_THIS_DIR))
    from utils import ssh_multiplex  # type: ignore
else:
    from . import ssh_multiplex


def _options_for(ssh_args: list[str]) -> list[str]:
    """
    Run `ssh -G <ssh_args>` and decide what `-o` flags to add. `ssh_args` is
    everything git passed us EXCEPT the trailing remote command — i.e. the
    options + destination, exactly as ssh -G expects.
    """
    try:
        cp = subprocess.run(
            ['ssh', '-G', *ssh_args],
            capture_output=True, text=True, timeout=5.0,
        )
    except (subprocess.TimeoutExpired, OSError):
        return []
    if cp.returncode != 0:
        return []
    probe: dict[str, str] = {}
    for line in cp.stdout.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        parts = line.split(None, 1)
        if len(parts) == 2:
            probe.setdefault(parts[0].lower(), parts[1])
    opts, _ = ssh_multiplex.options_to_add(probe)
    return opts


def main(argv: list[str]) -> int:
    args = argv[1:]
    extra: list[str] = []
    # Last arg is the remote command (`git-upload-pack '...'`); everything
    # before it is options + destination.
    if len(args) >= 2:
        try:
            extra = _options_for(args[:-1])
        except Exception:
            extra = []
    os.execvp('ssh', ['ssh', *extra, *args])


if __name__ == '__main__':
    raise SystemExit(main(sys.argv))
