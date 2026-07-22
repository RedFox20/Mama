"""Async build log: a daemon thread drains a queue to packages/mamabuild.log so write() never blocks
a build thread. ANSI is stripped for a clean, greppable file. Best-effort - a bad path or IO error
never breaks a build."""
import os, re, threading, queue

_ANSI_RE = re.compile(r'\x1b\[[0-9;]*[A-Za-z]')  # SGR colours + cursor moves, stripped for the log file


class AsyncLogWriter:
    def __init__(self, stream, flush_interval=1.0):
        """`stream`: an open, writable text stream this writer owns (open_build_log wraps the
        packages/mamabuild.log case; tests pass a capture stream). `flush_interval`: writes go to the
        buffered stream as they arrive (cheap) but are fsync-flushed only on an idle lull, so bursts are
        amortized yet confirmed output still lands on disk promptly (tail-able mid-build)."""
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


def open_build_log(path: str):
    """Open `path` (truncating) as an AsyncLogWriter, or None if it can't be created - the log must
    never break a build."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        return AsyncLogWriter(open(path, 'w', encoding='utf-8'))
    except OSError:
        return None
