#!/usr/bin/env python3
"""
GIT_SSH_COMMAND wrapper for mama.

Git invokes us as if we were ssh:
    mama_ssh.py [ssh-args] [-p port] [-l user] host command-on-remote...

We figure out the destination, run `ssh -G` once to read the user's effective
config for that host, decide which `-o` options need adding (multiplexing,
keepalives) WITHOUT overriding anything the user already has, then exec ssh
with the augmented args.

This wrapper is invoked many times (once per git op); it must be cheap. We
cache per-host options under $XDG_CACHE_HOME so repeated invocations during
the same mama run don't re-probe.

If anything goes wrong we still exec ssh with the original args — never
break the build because of multiplexing setup.
"""

from __future__ import annotations

import json
import os
import sys
import time

# Allow running as a standalone script without a parent package context.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if __package__ in (None, ''):
    sys.path.insert(0, os.path.dirname(_THIS_DIR))  # add mama/ parent to path
    from utils import ssh_multiplex  # type: ignore
else:
    from . import ssh_multiplex


_CACHE_TTL_SECONDS = 60 * 30  # 30 minutes per-host options cache


def _cache_path() -> str:
    base = os.environ.get('XDG_CACHE_HOME') or os.path.expanduser('~/.cache')
    d = os.path.join(base, 'mama')
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, 'ssh_host_options.json')


def _load_cache() -> dict:
    try:
        with open(_cache_path(), 'r') as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_cache(cache: dict) -> None:
    path = _cache_path()
    tmp = path + '.tmp'
    try:
        with open(tmp, 'w') as f:
            json.dump(cache, f)
        os.replace(tmp, path)
    except OSError:
        pass


def _options_for(user: str, host: str, port: str | None) -> list[str]:
    key = ssh_multiplex.host_key(user, host, port)
    cache = _load_cache()
    entry = cache.get(key)
    if entry and time.time() - entry.get('ts', 0) < _CACHE_TTL_SECONDS:
        return list(entry.get('opts', []))
    probe = ssh_multiplex.probe_ssh_config(user, host, port)
    opts, _ = ssh_multiplex.options_to_add(probe)
    cache[key] = {'ts': time.time(), 'opts': opts}
    _save_cache(cache)
    return opts


def main(argv: list[str]) -> int:
    args = argv[1:]
    extra: list[str] = []
    parsed = ssh_multiplex.parse_host_from_ssh_args(args)
    if parsed is not None:
        user, host, port = parsed
        try:
            extra = _options_for(user, host, port)
        except Exception:
            extra = []
    final = ['ssh', *extra, *args]
    os.execvp('ssh', final)


if __name__ == '__main__':
    raise SystemExit(main(sys.argv))
