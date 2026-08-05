"""Async build log: a daemon thread drains a queue to packages/mamabuild.log, so write() never blocks a
build thread. The writer strips ANSI codes, and a bad path or IO error never breaks a build."""
import atexit, os, re, threading, queue

from .paths import path_join

_ANSI_RE = re.compile(r'\x1b\[[0-9;]*[A-Za-z]')  # SGR colors + cursor moves, stripped for the log file


class AsyncLogWriter:
    def __init__(self, stream, flush_interval=1.0):
        """stream: an open, writable text stream this writer owns and closes
        flush_interval: seconds of idle lull before a flush, so bursts amortize and the log stays tail-able"""
        self._stream = stream
        self._flush_interval = flush_interval
        self._q: queue.Queue = queue.Queue()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def write(self, text: str):
        self._q.put(text)

    def _flush(self):
        try: self._stream.flush()
        except (OSError, ValueError): pass  # stream closed/broken mid-build: the log is best-effort

    def _loop(self):
        dirty = False
        while True:
            try:
                item = self._q.get(timeout=self._flush_interval)
            except queue.Empty:
                if dirty: self._flush(); dirty = False  # a lull -> persist the confirmed output so far
                continue
            if item is None: break
            try: self._stream.write(_ANSI_RE.sub('', item)); dirty = True
            except (OSError, ValueError): pass
        self._flush()

    def close(self):
        self._q.put(None)
        self._thread.join(timeout=2.0)
        try: self._stream.flush(); self._stream.close()
        except (OSError, ValueError): pass


_build_log = None

def open_build_log(path: str):
    """The one build log of this run, opened on the first call and reused after it. A run has several
    phases, and each one writes to the same log, so only the first open may truncate.
    Returns None when the file cannot be created: the log must never break a build."""
    global _build_log
    if _build_log is not None:
        return _build_log
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        _build_log = AsyncLogWriter(open(path, 'w', encoding='utf-8'))
    except OSError:
        return None
    atexit.register(close_build_log)  # every exit path drains it, and a display never owns it
    return _build_log


def open_run_log(workspaces_root: str, workspace: str):
    """Open the build log of this run under the workspace the root mamafile named. mamabuild calls this
    once, right after the root load, because that load is what names the workspace. Returns None when
    there is nowhere to write."""
    if not workspaces_root: return None
    return open_build_log(path_join(workspaces_root, workspace or 'packages', 'mamabuild.log'))


def get_build_log():
    """The build log this run opened, or None. A display reads it, it never opens one of its own."""
    return _build_log


def close_build_log():
    """Drain and close the build log of this run. Runs at process exit."""
    global _build_log
    if _build_log is not None:
        _build_log.close()
        _build_log = None
