import os, shlex, shutil, threading, queue, time, signal
import subprocess
from .system import System, console, error, report_subprocess, capture_to, capture_context


# Linux/macOS: we allocate a PTY for the child so git etc. still see a TTY
# (preserves progress output and isatty checks). The pty.openpty() syscall
# does NOT fork - it just creates a master/slave fd pair - so it's safe to
# call from a worker thread. subprocess.Popen does the actual fork via
# posix_spawn/vfork which is multi-thread-safe, unlike the older os.forkpty
# which Python 3.12 flags with a DeprecationWarning specifically because of
# the threaded-deadlock risk.
if not System.windows:
    import pty


READER_IDLE_TIMEOUT = 0.1  # seconds; how long to wait before flushing a \r-progress partial
READER_CHUNK = 8192


_procs_lock = threading.Lock()
_live_procs = set()   # live SubProcess instances; killed en masse by terminate_all() on Ctrl+C
_aborting = False     # set by terminate_all(): blocks new spawns so an interrupted build can't relaunch


class SubProcess:
    """
    Subprocess wrapper with optional line-by-line output capture.

    With ``io_func`` set, child's combined stdout+stderr is fed to ``io_func``
    one line at a time by a background reader thread. On UNIX a PTY is
    allocated so the child sees a TTY (gets coloured/progress output).

    Without ``io_func``, the child inherits the parent's stdout/stderr -
    used for things like `mama test` where output should flow directly.

    Replaces the previous os.fork/os.forkpty-based implementation which
    was unsafe in multi-threaded programs (Python 3.12 deprecation warning,
    real deadlocks under heavy parallel load).
    """
    def __init__(self, cmd, cwd=None, env=None, io_func=None):
        self.io_func = io_func
        self.status = None
        self.process = None
        self._reader_thread = None
        self._reader_exc = None        # exception raised inside io_func (re-raised in run())
        self._master_fd = None         # UNIX PTY master fd; None on Windows or no-io_func paths
        self._swallow_lf = False       # after \r-progress idle-flush, swallow a leading \n (or \r\n) in next chunk
        self._last_output = time.monotonic()  # bumped on every chunk; drives the idle-timeout watchdog
        self._group = False            # True: child leads its own group/session -> kill() tears down its whole tree
        self._killed = False           # set by kill(); close() only force-closes the pipe early when we killed

        env = env if env else os.environ.copy()
        args = shlex.split(cmd) if isinstance(cmd, str) else list(cmd)

        # Resolve the executable ourselves so we don't have to ask the shell
        # (avoids shell quoting/escaping pitfalls; same logic as before).
        executable = args[0]
        if os.path.isfile(executable):
            executable = os.path.abspath(executable)
        elif System.windows and os.path.isfile(executable + '.exe'):
            executable = os.path.abspath(executable + '.exe')
        else:
            resolved = shutil.which(executable)
            if not resolved:
                raise OSError(f"SubProcess failed to start: {executable} not found in PATH")
            executable = resolved
        args[0] = executable

        if io_func is None:
            # No capture: child inherits parent's stdio (terminal direct).
            self.process = subprocess.Popen(args, cwd=cwd, env=env)
            return

        if System.windows:
            # No PTY on Windows; merge stderr into stdout pipe. Binary mode so the
            # reader can byte-level split on \r as well as \n (ninja/cmake progress).
            self.process = subprocess.Popen(args, cwd=cwd, env=env,
                                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            self._group = True  # kill() uses taskkill /T to take down cmake's ninja/compiler subtree
        else:
            # Allocate a PTY pair; child gets the slave end as its stdin/stdout/stderr.
            self._master_fd, slave = pty.openpty()
            try:
                # start_new_session: child leads its own session so kill() can killpg the whole tree
                # (cmake -> ninja -> compilers), not just the spawned pid.
                self.process = subprocess.Popen(args, cwd=cwd, env=env, stdin=slave, stdout=slave,
                                                stderr=slave, close_fds=True, start_new_session=True)
                self._group = True
            finally:
                os.close(slave) # parent doesn't need the slave once Popen has it

        # io_func runs on the reader thread; carry the caller's console-capture context onto it so its
        # console() lines feed the owning display task instead of leaking above the live region.
        self._capture_ctx = capture_context()
        self._reader_thread = threading.Thread(target=self._read_loop, daemon=True)
        self._reader_thread.start()


    def _read_loop(self):
        try:
            with capture_to(*self._capture_ctx):  # io_func's console() -> owning task, not above the region
                fd = self._master_fd if self._master_fd is not None else \
                     (self.process.stdout.fileno() if self.process.stdout else None)
                if fd is not None: self._read_loop_queued(fd)
        except Exception as e:
            self._reader_exc = e  # captured so run() can surface it; don't crash the reader thread


    def _read_loop_queued(self, fd):
        """One reader for both PTY (UNIX) and pipe (Windows): a pump thread does the blocking
        os.read(fd) and hands chunks to a queue; this drain loop turns them into lines with the
        \\r-progress idle-flush (queue.get timeout). Decoupling the read from io_func means a slow
        consumer (or GIL contention from CPU sampling) never stalls os.read, so the child's PTY/pipe
        can't fill and block it - the footgun that made big library output trickle in slowly."""
        chunks: queue.Queue = queue.Queue()
        def pump():
            while True:
                try: chunk = os.read(fd, READER_CHUNK)
                except OSError: chunk = b''  # EIO on a closed PTY slave / closed pipe = EOF
                if chunk: self._last_output = time.monotonic()  # reset idle watchdog AS DATA ARRIVES, not
                chunks.put(chunk)                                # when the drain processes it (drain can lag under load)
                if not chunk: break
        threading.Thread(target=pump, daemon=True).start()
        buf = bytearray()
        while True:
            try:
                chunk = chunks.get(timeout=READER_IDLE_TIMEOUT)
            except queue.Empty:
                self._drain_buffer(buf, idle=True); continue
            if not chunk: break
            buf.extend(chunk)
            self._drain_buffer(buf)
        self._drain_buffer(buf, eof=True)


    def _drain_buffer(self, buf:bytearray, idle=False, eof=False):
        """Emit \\r- and \\n-delimited lines from buf. \\r alone is progress;
        \\r\\n and bare \\n are line endings (trailing \\r stripped). A \\r at
        buf end waits for more data unless idle/eof, then sets _swallow_lf so
        the next chunk's leading \\n or \\r\\n (via PTY ONLCR) is consumed.

        Scans with a moving `pos` cursor and trims consumed bytes in one
        trailing `del` instead of a `del buf[:k]` per emitted line."""
        n = len(buf)
        pos = 0
        if self._swallow_lf and n:
            if n >= 2 and buf[0] == 0x0d and buf[1] == 0x0a: pos = 2 # 0x0d = \r, 0x0a = \n
            elif buf[0] == 0x0a: pos = 1
            self._swallow_lf = False
        while True:
            cr = buf.find(b'\r', pos)
            if cr >= 0:
                if cr + 1 < n and buf[cr + 1] != 0x0a:
                    self._emit_io_out(buf[pos:cr])
                    pos = cr + 1
                    continue
                if cr + 1 == n and (idle or eof):
                    self._emit_io_out(buf[pos:cr])
                    pos = cr + 1
                    self._swallow_lf = True
                    continue
            nl = buf.find(b'\n', pos)
            if nl >= 0:
                end = nl - 1 if nl > pos and buf[nl - 1] == 0x0d else nl
                self._emit_io_out(buf[pos:end])
                pos = nl + 1
                continue
            break
        if eof and pos < n:
            self._emit_io_out(buf[pos:n]); pos = n
        if pos: del buf[:pos]


    def _emit_io_out(self, buf:bytearray):
        if self.io_func:
            try:
                self.io_func(self, buf.decode('utf-8', errors='replace'))
            except Exception as e:
                # Capture so run() can surface it. Don't crash the reader thread.
                self._reader_exc = e


    def write(self, text: str):
        """Send `text` to the child's stdin (used for interactive prompts
        like SSH host-key acceptance)."""
        data = text.encode('utf-8')
        if self._master_fd is not None:
            os.write(self._master_fd, data)
        elif self.process and self.process.stdin and not self.process.stdin.closed:
            try:
                self.process.stdin.write(data)
                self.process.stdin.flush()
            except (BrokenPipeError, OSError):
                pass


    @staticmethod
    def terminate_all():
        """Ctrl+C handler: block new spawns and kill every live child so a parallel build unwinds
        fast instead of the thread pool blocking on in-flight compiles. Idempotent."""
        global _aborting
        with _procs_lock:  # set the flag + snapshot atomically so a concurrent run() can't slip a
            _aborting = True  # child past us (it re-checks _aborting under the same lock after spawning)
            procs = list(_live_procs)
        for p in procs:
            try: p.kill()
            except Exception: pass

    @staticmethod
    def clear_abort():
        """Re-arm spawning after a terminated build (so a later run in the same process starts clean)."""
        global _aborting
        _aborting = False

    def kill(self):
        p = self.process
        if not p or p.poll() is not None:
            return
        self._killed = True
        if self._group:
            self._kill_tree(p)  # build/clone child: take down its whole subtree, not just the pid
            return
        try:
            p.terminate()
            p.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            try:
                p.kill()
                p.wait(timeout=1.0)
            except Exception:
                pass
        except Exception:
            pass

    def _kill_tree(self, p):
        """Kill the child AND its descendants (ninja + compilers). A plain terminate()/kill() hits
        only the spawned cmake/git pid; on Windows TerminateProcess and on UNIX a single SIGKILL both
        leave the compiler grandchildren running. taskkill /T walks the child tree; killpg signals the
        whole session. Falls back to a single-process kill if the tree call fails.
        Raw subprocess.run (not SubProcess.run): the killer must not register in _live_procs nor be
        blocked by the _aborting guard (it runs precisely while aborting)."""
        try:
            if System.windows:
                subprocess.run(['taskkill', '/F', '/T', '/PID', str(p.pid)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
            else:
                os.killpg(os.getpgid(p.pid), signal.SIGKILL)
        except Exception:
            try: p.kill()
            except Exception: pass
        try: p.wait(timeout=2.0)
        except Exception: pass


    def close(self):
        self.kill()  # no-op if the child already exited; sets self._killed if it had to kill a live one
        win_out = self.process.stdout if (System.windows and self.process) else None
        # Force the Windows read-end shut ONLY when we killed the child: its grandchildren (ninja/compilers)
        # may still hold the write end, so the pump would block in os.read forever. On a CLEAN exit the write
        # end is already closed, so closing here would race the pump and DROP the final buffered lines (e.g.
        # the compiler error that failed the build) - instead drain first (join) and close after.
        if win_out and self._killed:
            try: win_out.close()
            except OSError: pass
        # Reader thread drains its queue then exits on EOF (Windows pipe closed, or UNIX PTY master closed
        # below). Join so all trailing output reaches io_func before we return.
        if self._reader_thread:
            self._reader_thread.join(timeout=2.0)
            self._reader_thread = None
        if win_out and not win_out.closed:  # clean-exit path: now that the reader has drained, close it
            try: win_out.close()
            except OSError: pass
        if self._master_fd is not None:
            try:
                os.close(self._master_fd)
            except OSError:
                pass
            self._master_fd = None


    def try_wait(self):
        """Returns the exit status if the child has finished, else None.
        Kept for backwards-compat with callers that used the old polling API."""
        if self.process is None:
            return self.status
        rc = self.process.poll()
        if rc is not None:
            self.status = rc
        return self.status


    def _wait_idle(self, timeout, idle_timeout):
        """Wait for the child, killing it if it's silent for `idle_timeout` s (or exceeds total
        `timeout`). The idle bound catches a git op stuck on an auth prompt / hung server without
        aborting a slow-but-streaming clone. Raises TimeoutExpired on either bound."""
        start = time.monotonic()
        while True:
            try:
                return self.process.wait(timeout=0.25)
            except subprocess.TimeoutExpired:
                pass
            now = time.monotonic()
            if timeout is not None and now - start > timeout:
                self.kill(); raise subprocess.TimeoutExpired(self.process.args, timeout)
            if now - self._last_output > idle_timeout:
                self.kill(); raise subprocess.TimeoutExpired(self.process.args, idle_timeout)

    @staticmethod
    def run(cmd, cwd=None, env=None, io_func=None, timeout=None, idle_timeout=None):
        """
        Runs `cmd` and returns its exit status.
        - cmd:     command string (shlex.split) or list of args.
        - cwd:     working directory for the child.
        - env:     environment dict, defaults to os.environ.
        - io_func: callback `(SubProcess, line:str)` for each output line;
                   if None, child inherits parent's std streams.
        - timeout: kill the child after this many seconds total (raises TimeoutExpired).
        - idle_timeout: kill if silent this many seconds (raises TimeoutExpired). Needs io_func set.
                   For network git ops that may hang on a prompt; a streaming clone is never killed.
        """
        if _aborting: raise KeyboardInterrupt('build aborted')  # fast path: don't even spawn after Ctrl+C
        p = SubProcess(cmd, cwd=cwd, env=env, io_func=io_func)
        pid = p.process.pid if p.process else None
        with _procs_lock:
            if _aborting:  # aborted mid-spawn: kill this child now instead of leaking it past terminate_all
                p.close(); raise KeyboardInterrupt('build aborted')
            _live_procs.add(p)  # registered so terminate_all() can kill it on Ctrl+C
        if pid is not None: report_subprocess(pid, True)  # live CPU sampling for the owning display task
        try:
            try:
                if idle_timeout is not None:
                    p.status = p._wait_idle(timeout, idle_timeout)
                else:
                    p.status = p.process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                p.kill()
                raise
        finally:
            with _procs_lock: _live_procs.discard(p)
            if pid is not None: report_subprocess(pid, False)
            p.close()
            if p._reader_exc is not None:
                raise p._reader_exc
        return p.status


def execute(command, echo=False, throw=True):
    """
    Executes a command and returns the status code.
    - command: command string
    - echo: if True, prints the command to console
    - throw: if True, throws exception on status_code != 0
    - returns: status code
    """
    if echo: console(command)
    retcode = os.system(command)
    if throw and retcode != 0:
        raise RuntimeError(f'{command} failed with return code {retcode}')
    return retcode


def execute_piped(command, cwd=None, timeout=None, throw=True):
    """
    Executes a command and returns the piped outout string
    - command: command string
    - cwd: working dir for the subprocess
    - timeout: timeout in seconds
    - throw: if True, throws exception on status_code != 0
    - returns: output string or None if throw=False
    """
    if not isinstance(command, list):
        command = shlex.split(command)
    try:
        cp = subprocess.run(command, stdout=subprocess.PIPE, cwd=cwd, timeout=timeout)
        return cp.stdout.decode('utf-8').rstrip()
    except Exception as e:
        if throw:
            raise RuntimeError(f'subprocess.Run {command} failed: {e}')
        else:
            return None


def execute_echo(cwd, cmd, exit_on_fail=False, env=None, quiet=False):
    """
    Wrapper around SubProcess.run(), by default throws if exit_status != 0
    - cwd: working dir for the subprocess
    - cmd: command string
    - exit_on_fail: if True, exits the application with exit_status
    - env: overrrides the environment for the subprocess, default is os.environ
    - quiet: if True, drop the child's output entirely (it still runs and is exit-checked)
    """
    # Inside a scheduled build phase a capture sink is active: route the child's output through console()
    # so a custom build()'s commands land in the owning display task (and the log) instead of tearing the
    # live region. Outside it (serial path, interactive run/gdb/test post-pass) keep stdio direct - the
    # child needs the real terminal for prompts, and there's nowhere to capture to anyway.
    if quiet:               io = lambda p, line: None                # caller asked for silence: drop output
    elif capture_context()[0] is not None: io = lambda p, line: console(line)
    else:                   io = None
    exit_status = -1
    throw_on_fail = not exit_on_fail
    try:
        exit_status = SubProcess.run(cmd, cwd, env=env, io_func=io)
    except:
        error(f'SubProcess exited cwd={cwd} cmd={cmd}')
        if throw_on_fail:
            raise
    if exit_status != 0:
        if throw_on_fail:
            raise RuntimeError(f'Execute {cmd} failed with error: {exit_status}')
        elif exit_on_fail:
            exit(exit_status)


def execute_piped_echo(cwd, cmd, echo=True, env=None, out=None):
    """
    Wrapper around SubProcess.run(), returns status code with piped output (status, output).
    - cwd: working dir for the subprocess
    - cmd: command string
    - echo: if True, also prints the output to console
    - env: overrrides the environment for the subprocess, default is os.environ
    - out: optional `(line) -> None` sink; when set, lines go there instead of being printed
    - returns: (exit_status, output_string)
    """
    lines = []  # list + join, NOT output += line: the latter is O(n^2) over a big build's output
    def handle_output(p:SubProcess, line:str):
        if out:    out(line)
        elif echo: print(line)
        lines.append(line)
    try:
        exit_status = SubProcess.run(cmd, cwd, env=env, io_func=handle_output)
        return (exit_status, '\n'.join(lines))
    except Exception as e:
        lines.append(str(e))
        return (-1, '\n'.join(lines))
