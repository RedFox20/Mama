"""Async build log: a daemon thread drains a queue to packages/mamabuild.log so write() never blocks
a build thread. ANSI is stripped for a clean, greppable file. Best-effort - a bad path or IO error
never breaks a build."""
import os, re, threading, queue

_ANSI_RE = re.compile(r'\x1b\[[0-9;]*[A-Za-z]')  # SGR colours + cursor moves, stripped for the log file


class AsyncLogWriter:
    def __init__(self, stream):
        """`stream`: an open, writable text stream this writer owns (open_build_log wraps the
        packages/mamabuild.log case; tests pass a capture stream)."""
        self._stream = stream
        self._q: queue.Queue = queue.Queue()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def write(self, text: str):
        self._q.put(text)

    def _loop(self):
        while True:
            item = self._q.get()
            if item is None: break
            try: self._stream.write(_ANSI_RE.sub('', item))
            except (OSError, ValueError): pass  # stream closed/broken mid-build: the log is best-effort

    def close(self):
        self._q.put(None)
        self._thread.join(timeout=2.0)
        try: self._stream.flush(); self._stream.close()
        except (OSError, ValueError): pass


def open_build_log(path: str):
    """Open `path` (truncating) as an AsyncLogWriter, or None if it can't be created - the log must
    never break a build."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        return AsyncLogWriter(open(path, 'w', encoding='utf-8'))
    except OSError:
        return None
