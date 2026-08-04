#!/usr/bin/env python3
"""GIT_SSH_COMMAND wrapper for mama: probe `ssh -G`, add the multiplex/keepalive options the user has not
set, then exec ssh. On any error it execs ssh with the original args: multiplex setup must never break a build."""

from __future__ import annotations

import os
import sys

# Standalone-script mode: add the package's PARENT to sys.path, never `<...>/mama` itself,
# because `mama/types/` would then shadow the stdlib `types` module on any `from types import ...`.
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
    # the last arg is the remote command, everything before it is options + destination, which is what ssh -G expects
    if len(args) >= 2:
        try:
            probe = ssh_multiplex.probe_ssh_config(args[:-1])
            extra, _ = ssh_multiplex.options_to_add(probe)
        except Exception:
            pass
    os.execvp('ssh', ['ssh', *extra, *args])


if __name__ == '__main__':
    raise SystemExit(main(sys.argv))
