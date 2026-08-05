"""Cross-process advisory lock on a directory, held as an flock/msvcrt lock on a sidecar file under
`.mama/locks`. The kernel releases it when the fd closes or the process dies, so a crash never sticks."""
import contextlib, os, time
from .paths import workspace_mama_dir
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
    """Hold an exclusive cross-process lock on `lock_dir` for the `with` block. Yields True on acquire, else
    False on timeout, and a timed-out caller still runs, unlocked. Always releases on exit.

    The sidecar lives in the `.mama/locks` dir of the workspace, never inside lock_dir. A lock file removed
    while a process holds it keeps that inode for that process alone. The next opener creates a fresh inode
    and takes it, so two processes both believe they hold the lock. `mama wipe` removes a whole dep dir, so
    a sidecar inside one would hit exactly that.

    lock_dir: the directory to guard. Different lock_dirs never contend, so loads of different deps stay parallel
    timeout: seconds to wait for the lock
    poll: seconds between lock attempts"""
    lock_dir = os.path.normpath(lock_dir)
    locks = workspace_mama_dir(os.path.dirname(lock_dir) or '.', 'locks')
    os.makedirs(locks, exist_ok=True)
    fd = os.open(os.path.join(locks, f'{os.path.basename(lock_dir)}.lock'), os.O_RDWR | os.O_CREAT, 0o644)
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
