#!/usr/bin/env python3
"""
GIT_SSH_COMMAND wrapper for mama.

Git invokes us as if we were ssh:
    mama_ssh.py [ssh-args] [user@]host command-on-remote

We hand the same args (minus the trailing remote command) to `ssh -G`,
which gives us the user's effective config for that destination. Then we
decide which extra `-o` flags to add (multiplexing, keepalives) WITHOUT
overriding anything the user already configured, and exec ssh with the
augmented args.

If anything goes wrong we still exec ssh with the original args. Never
break a build because of multiplexing setup.
"""

from __future__ import annotations

import os
import sys

# Allow running as a standalone script, not just as a package module.
# Important: do NOT put `<...>/mama` on sys.path - `mama/types/` would then
# shadow Python's stdlib `types` module the moment anything (e.g. contextlib)
# does `from types import ...`. Add the package's PARENT instead, so that
# `mama.utils.ssh_multiplex` resolves as a normal qualified import.
if __package__ in (None, ''):
    try:
        from mama.utils import ssh_multiplex
    except ImportError:
        _MAMA_PARENT = os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__))))
        sys.path.insert(0, _MAMA_PARENT)
        from mama.utils import ssh_multiplex
else:
    from . import ssh_multiplex


def main(argv: list[str]) -> int:
    args = argv[1:]
    extra: list[str] = []
    # Last arg is the remote command (`git-upload-pack '...'`); everything
    # before it is options + destination, which is exactly what ssh -G expects.
    if len(args) >= 2:
        try:
            probe = ssh_multiplex.probe_ssh_config(args[:-1])
            extra, _ = ssh_multiplex.options_to_add(probe)
        except Exception:
            pass
    os.execvp('ssh', ['ssh', *extra, *args])


if __name__ == '__main__':
    raise SystemExit(main(sys.argv))
