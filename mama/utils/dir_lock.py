"""Cross-process advisory lock on a directory, held as an flock/msvcrt lock on a sidecar `.mama.lock` file.
The kernel releases the lock when the fd closes or the process dies, so a crash can never leave it stuck."""
import contextlib, os, time
from .system import System, warning

if System.windows:
    import msvcrt
    def _try_lock(fd) -> bool:
        try:
            os.lseek(fd, 0, os.SEEK_SET); msvcrt.locking(fd, msvcrt.LK_NBLCK, 1); return True
        except OSError:
            return False
    def _unlock(fd):
        try:
            os.lseek(fd, 0, os.SEEK_SET); msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
else:
    import fcntl
    def _try_lock(fd) -> bool:
        try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB); return True
        except OSError: return False
    def _unlock(fd):
        try: fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError: pass


@contextlib.contextmanager
def interprocess_dir_lock(lock_dir: str, timeout: float, poll: float = 0.1):
    """Hold an exclusive cross-process lock on `lock_dir` for the `with` block. Yields True on acquire, or
    False when the acquire timed out after `timeout` seconds. A timed-out caller still runs, unlocked.
    Always releases on exit. Different `lock_dir`s never contend, so loads of different deps stay parallel."""
    # The sidecar lives beside lock_dir, never inside it: a reclone wipe removes the whole lock_dir, and a lock
    # file deleted while held unlinks its inode, so the next opener makes a fresh inode and exclusion silently
    # breaks. The parent location survives a wipe of the directory it guards.
    lock_dir = os.path.normpath(lock_dir)
    parent = os.path.dirname(lock_dir) or '.'
    os.makedirs(parent, exist_ok=True)
    fd = os.open(os.path.join(parent, f'.{os.path.basename(lock_dir)}.mama.lock'), os.O_RDWR | os.O_CREAT, 0o644)
    acquired = False
    try:
        deadline = time.monotonic() + timeout
        while not (acquired := _try_lock(fd)) and time.monotonic() < deadline:
            time.sleep(poll)
        if not acquired:
            warning(f'  - dir lock on {lock_dir} timed out after {timeout:.0f}s; proceeding without it')
        yield acquired
    finally:
        if acquired: _unlock(fd)
        os.close(fd)
