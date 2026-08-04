"""Cooperative shutdown for a build that must stop: a first failure, or Ctrl+C. `request()` only sets a flag,
so nothing new starts. Mama then asks each running child to stop and kills only one that ignores the grace period."""

import threading

GRACE_SECONDS = 1.0   # seconds a live child gets to stop on its own before mama kills it
POLL_INTERVAL = 0.05  # seconds between the checks for a child to exit


class BuildAborted(BaseException):
    """A gate raises this when the build stops. It is a BaseException, not an Exception, because an
    `except Exception` on the build path must not catch a shutdown and continue to work."""


_lock = threading.Lock()
_requested = threading.Event()
_reason = ''


def request(reason: str):
    """Stage 1: stop every job that has not started yet. Idempotent. The FIRST reason stays, because
    that is the cause the user must read. A later failure is only a consequence of it."""
    global _reason
    with _lock:
        if _requested.is_set(): return
        _reason = reason  # set the reason first, so a thread that sees the flag also reads a real one
        _requested.set()


def requested() -> bool: return _requested.is_set()
def reason() -> str: return _reason


def check():
    """Gate: raise if the build stops. Call this where new work would otherwise start."""
    if _requested.is_set(): raise BuildAborted(f'build stopped: {_reason}')


def clear():
    """Re-arm for a later build in the same process (a `mama <host> build` bootstrap, or a test)."""
    global _reason
    with _lock:
        _reason = ''
        _requested.clear()
