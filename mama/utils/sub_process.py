import os, shlex, shutil, threading, queue, time, signal
import subprocess  # psutil is deferred, see _kill_group
from functools import lru_cache
from . import abort
from .system import System, console, error, report_subprocess, capture_to, capture_context


# UNIX children get a PTY, so git etc. keep their progress output and isatty checks. pty.openpty() does
# NOT fork, it only makes an fd pair, so a worker thread can call it. Popen forks via posix_spawn, which
# is multi-thread safe, unlike os.forkpty, which Python 3.12 deprecates for deadlock risk under threads.
if not System.windows:
    import pty


READER_IDLE_TIMEOUT = 0.1  # seconds to wait before flushing a \r-progress partial
READER_CHUNK = 8192


_procs_lock = threading.Lock()
_live_procs = set()   # live SubProcess instances. terminate_all() stops every one of them


def _descendants(pid) -> list:
    """Every descendant pid of `pid` on Windows, and [] on UNIX, where a process group needs none.

    Read this while the root still lives. A killed root leaves its grandchildren with no tree to walk,
    and Windows has no process group to sweep them by."""
    if not System.windows: return []
    try:
        import psutil
        return [c.pid for c in psutil.Process(pid).children(recursive=True)]
    except Exception:
        return []


def _kill_pid(pid):
    """Kill one pid, and ignore a pid that already left."""
    try:
        import psutil
        psutil.Process(pid).kill()
    except Exception:
        pass


def _kill_group(gid) -> bool:
    """Hard-kill a whole process group (UNIX) or a pid's process tree (Windows). True when the kill
    reached the root. False when it was already gone, which the caller treats as a no-op.

    Windows has no process group kill, so psutil walks the tree instead. psutil is already a mama
    dependency, and it replaces a `taskkill /F /T` child that cost about 300ms per kill. Spawning a
    process to stop one is the wrong move anyway, because this runs precisely while mama aborts.

    psutil imports here, not at the top of the module. It costs about 32ms, only a Windows kill
    needs it, and a kill is rare."""
    try:
        if System.windows:
            import psutil
            root = psutil.Process(gid)
            for child in root.children(recursive=True):  # children first, so the root spawns no more
                try: child.kill()
                except psutil.Error: pass
            root.kill()
        else:
            os.killpg(gid, signal.SIGKILL)
        return True
    except Exception:
        return False


@lru_cache(maxsize=None)
def resolve_executable(name: str, cwd: str) -> str:
    """Absolute path of the program `name`, or '' when nothing on PATH matches. Memoized, because
    shutil.which reads every PATH directory and costs 2ms for `cmake`, and a build spawns hundreds of
    children. PATH holds still for one mama run. cwd is part of the key, because a relative name and,
    on Windows, a bare name both resolve against the working directory first.
    cwd: the working directory the name resolves against, from os.getcwd()"""
    if os.path.isfile(name): return os.path.abspath(name)
    if System.windows and os.path.isfile(name + '.exe'): return os.path.abspath(name + '.exe')
    return shutil.which(name) or ''


class SubProcess:
    """Subprocess wrapper with optional line-by-line output capture. With `io_func` set, a background
    reader thread feeds the child's combined stdout+stderr to `io_func` one line at a time, and on UNIX
    the child runs on a PTY, so it prints colored/progress output. Without `io_func`, the child inherits
    the parent's stdout/stderr, for commands whose output must flow directly."""
    def __init__(self, cmd, cwd=None, env=None, io_func=None):
        self.io_func = io_func
        self.status = None
        self.process = None
        self._reader_thread = None
        self._reader_exc = None        # exception raised inside io_func (re-raised in run())
        self._master_fd = None         # UNIX PTY master fd, None on Windows or no-io_func paths
        self._swallow_lf = False       # after \r-progress idle-flush, swallow a leading \n (or \r\n) in next chunk
        self._last_output = time.monotonic()  # bumped on every chunk, drives the idle-timeout watchdog
        self._group = False            # True: child leads its own group/session -> kill() tears down its whole tree
        self._killed = False           # set by kill(). close() only force-closes the pipe early after a kill

        env = env if env else os.environ.copy()
        args = shlex.split(cmd) if isinstance(cmd, str) else list(cmd)

        # Resolve the executable here instead of asking a shell, so no shell quoting or escaping applies.
        executable = resolve_executable(args[0], os.getcwd())
        if not executable:
            raise OSError(f"SubProcess failed to start: {args[0]} not found in PATH")
        args[0] = executable

        if io_func is None:
            # No capture: child inherits parent's stdio (terminal direct).
            self.process = subprocess.Popen(args, cwd=cwd, env=env)
            return

        if System.windows:
            # No PTY on Windows: merge stderr into the stdout pipe, binary mode so the reader can split on \r.
            # CREATE_NEW_PROCESS_GROUP: the child leads its own group, so interrupt() can send it a console
            # CTRL_BREAK without a signal to mama itself, and a console Ctrl+C stops at mama (matches UNIX):
            # mama owns the shutdown and relays it, instead of a race with the child for the same signal.
            self.process = subprocess.Popen(args, cwd=cwd, env=env, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
                                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            self._group = True  # kill() uses taskkill /T to take down cmake's ninja/compiler subtree
        else:
            # Allocate a PTY pair. The child gets the slave end as its stdin/stdout/stderr.
            self._master_fd, slave = pty.openpty()
            try:
                # start_new_session: the child leads its own session, so kill() can killpg the whole tree, not one pid
                self.process = subprocess.Popen(args, cwd=cwd, env=env, stdin=slave, stdout=slave,
                                                stderr=slave, close_fds=True, start_new_session=True)
                self._group = True
            finally:
                os.close(slave) # the parent does not need the slave once Popen has it

        # carry the caller's console-capture context onto the reader thread, so io_func's console() lines
        # feed the owning display task instead of leaking above the live region
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
            self._reader_exc = e  # captured so run() can surface it, do not crash the reader thread


    def _read_loop_queued(self, fd):
        """One reader for both PTY (UNIX) and pipe (Windows): a pump thread does the blocking os.read(fd)
        and hands chunks to a queue, and this drain loop turns them into lines with the \\r-progress
        idle-flush. A slow io_func never stalls os.read, so the child's PTY/pipe cannot fill and block."""
        chunks: queue.Queue = queue.Queue()
        def pump():
            while True:
                try: chunk = os.read(fd, READER_CHUNK)
                except OSError: chunk = b''  # EIO on a closed PTY slave / closed pipe = EOF
                if chunk: self._last_output = time.monotonic()  # reset the idle watchdog AS DATA ARRIVES, the drain can lag
                chunks.put(chunk)
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
        """Emit \\r- and \\n-delimited lines from buf. A lone \\r is progress, \\r\\n and bare \\n end a line.
        A \\r at buf end waits for more data unless idle/eof, then sets _swallow_lf so the next chunk's
        leading \\n or \\r\\n (via PTY ONLCR) is consumed. Scans with a moving `pos` cursor and trims
        consumed bytes in one trailing `del`, not a `del buf[:k]` per emitted line."""
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
                self._reader_exc = e  # captured so run() can surface it, do not crash the reader thread


    def write(self, text: str):
        """Send `text` to the child's stdin, for interactive prompts like SSH host-key acceptance."""
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
    def terminate_all(reason: str, grace=abort.GRACE_SECONDS):
        """Stop the build in three stages. Idempotent.
        1. Set the abort flag, so nothing new spawns and every phase gate closes.
        2. Ask each live child to stop, as a Ctrl+C does, then wait `grace` seconds.
        3. Kill each child that ignored the request.
        The grace lets git and ninja remove their partial files, which a hard kill leaves behind for the
        next build to trip on."""
        # the flag and the snapshot go under one lock, so a concurrent run() cannot register a child this
        # snapshot misses: run() re-checks the flag under the same lock after it spawns
        with _procs_lock:
            abort.request(reason)
            procs = list(_live_procs)
        # read each group id NOW, while its leader lives: getpgid() fails once the pid is gone, and stage 3 needs the group
        groups = [g for g in (p.group_id() for p in procs) if g]
        orphans = [pid for g in groups for pid in _descendants(g)]  # windows: no group survives the root
        for p in procs: p.interrupt()
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            with _procs_lock: waiting = bool(_live_procs)
            if not waiting: break  # every child stopped on its own
            time.sleep(abort.POLL_INTERVAL)
        with _procs_lock: procs = list(_live_procs)
        for p in procs:
            try: p.kill()
            except Exception: pass
        # Sweep the groups too: a GRANDCHILD can miss the group signal (mid-exec when it lands) and run on
        # with nobody left to stop it. A group whose members all exited is empty and this is a no-op.
        for gid in groups: _kill_group(gid)
        # On Windows that grandchild is now an orphan, and a dead root can no longer name its tree. The
        # snapshot taken above is the only list of it left.
        for pid in orphans: _kill_pid(pid)

    @staticmethod
    def clear_abort():
        """Re-arm spawning after a stopped build (so a later run in the same process starts clean)."""
        abort.clear()

    def interrupt(self):
        """Ask the child and its whole tree to stop, as a Ctrl+C does, so it can remove its own leftovers.
        UNIX sends SIGINT to its session, Windows a console CTRL_BREAK to its process group. A Windows
        child in mama's own group (an interactive command) gets no signal, because mama cannot signal it
        without a signal to itself. kill() stops that one."""
        p = self.process
        if not p or p.poll() is not None: return
        try:
            if self._group and System.windows: os.kill(p.pid, signal.CTRL_BREAK_EVENT)
            elif self._group: os.killpg(os.getpgid(p.pid), signal.SIGINT)
            elif not System.windows: p.send_signal(signal.SIGINT)
        except Exception: pass  # a child that exited between the poll and the signal

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

    def group_id(self):
        """The child's process group, or None. Windows has no group kill, so it reports the pid that
        taskkill /T walks the tree from."""
        p = self.process
        if not p or not self._group: return None
        if System.windows: return p.pid
        try: return os.getpgid(p.pid)
        except Exception: return None

    def _kill_tree(self, p):
        """Kill the child AND its descendants: a plain terminate()/kill() hits only the spawned pid and
        leaves the compiler grandchildren running. Uses a single-process kill when the group call fails."""
        if not _kill_group(self.group_id() or p.pid):
            try: p.kill()
            except Exception: pass
        try: p.wait(timeout=2.0)
        except Exception: pass


    @staticmethod
    def _close_quietly(stream):
        """Close a stream and ignore the error. A caller starts this on its own thread when a close can block."""
        try: stream.close()
        except OSError: pass


    def close(self):
        self.kill()  # no-op if the child already exited, sets self._killed if it had to kill a live one
        win_out = self.process.stdout if (System.windows and self.process) else None
        # Force the Windows read-end shut ONLY after a kill: grandchildren may still hold the write end, so
        # the pump would block in os.read forever. On a CLEAN exit closing here would race the pump and
        # DROP the final buffered lines (the compiler error), so drain first (join) and close after.
        if win_out and self._killed:
            try: win_out.close()
            except OSError: pass
        # the reader drains its queue then exits on EOF: join so all trailing output reaches io_func before we return
        drained = True
        if self._reader_thread:
            self._reader_thread.join(timeout=2.0)
            drained = not self._reader_thread.is_alive()
            self._reader_thread = None
        if win_out and not win_out.closed:  # clean-exit path: now that the reader has drained, close it
            # A reader still alive means the pump still blocks in os.read on this handle. A close waits for
            # that read, and a grandchild that outlived the child holds the write end open, so never wait here.
            if drained:
                try: win_out.close()
                except OSError: pass
            else:
                threading.Thread(target=self._close_quietly, args=(win_out,), daemon=True).start()
        if self._master_fd is not None:
            try:
                os.close(self._master_fd)
            except OSError:
                pass
            self._master_fd = None


    def try_wait(self):
        """Returns the exit status when the child has finished, else None.
        Kept for backward compatibility with callers of the old polling API."""
        if self.process is None:
            return self.status
        rc = self.process.poll()
        if rc is not None:
            self.status = rc
        return self.status


    def _wait_idle(self, timeout, idle_timeout):
        """Wait for the child. Kill it when it stays silent for `idle_timeout` seconds, or exceeds the
        total `timeout`. The idle bound catches a git op stuck on an auth prompt or a hung server
        without aborting a slow-but-streaming clone. Raises TimeoutExpired on either bound."""
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
        """Runs `cmd` and returns its exit status.
        cmd: command string (shlex.split) or list of args
        cwd: working directory for the child
        env: environment dict, defaults to os.environ
        io_func: callback `(SubProcess, line:str)` per output line. If None, the child inherits the parent's std streams
        timeout: kill the child after this many seconds total (raises TimeoutExpired)
        idle_timeout: kill the child when silent this many seconds (raises TimeoutExpired). Needs io_func
                      set. For a git op that can hang on a prompt, a streaming clone is never killed."""
        abort.check()  # fast path: do not even spawn while the build stops
        p = SubProcess(cmd, cwd=cwd, env=env, io_func=io_func)
        pid = p.process.pid if p.process else None
        with _procs_lock:
            if abort.requested():  # stopped mid-spawn: close this child now, so terminate_all cannot miss it
                p.close(); abort.check()
            _live_procs.add(p)  # registered so terminate_all() can stop it
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
    """Executes a command and returns the status code.
    command: command string
    echo: if True, prints the command to console
    throw: if True, throws an exception on status_code != 0
    os.system, so the child gets the real terminal and a shell: for INTERACTIVE commands only (`code`,
    a sudo prompt). It inherits stdout and stderr, so it tears the live display and no filter can reach
    its output. Use execute_echo or SubProcess.run for anything on the build path."""
    if echo: console(command)
    retcode = os.system(command)
    if throw and retcode != 0:
        raise RuntimeError(f'{command} failed with return code {retcode}')
    return retcode


def execute_piped(command, cwd=None, timeout=None, throw=True):
    """Executes a command and returns the piped output string, or None on failure when throw=False.
    command: command string
    cwd: working dir for the subprocess
    timeout: timeout in seconds
    throw: if True, raises on a spawn error or timeout. A non-zero exit status never raises here.
    stderr is CAPTURED, never inherited: a child that writes straight to the terminal bypasses every mama
    filter and tears the live display, which redraws by counting the lines it wrote itself. The caller
    wants stdout, and a real failure still surfaces through the clone or fetch that follows."""
    if not isinstance(command, list):
        command = shlex.split(command)
    try:
        cp = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd, timeout=timeout)
        return cp.stdout.decode('utf-8').rstrip()
    except Exception as e:
        if throw:
            raise RuntimeError(f'subprocess.Run {command} failed: {e}')
        else:
            return None


def execute_echo(cwd, cmd, exit_on_fail=False, env=None, quiet=False):
    """Wrapper around SubProcess.run(), by default throws if exit_status != 0.
    cwd: working dir for the subprocess
    cmd: command string
    exit_on_fail: if True, exits the application with exit_status
    env: overrides the environment for the subprocess, default is os.environ
    quiet: if True, drops the child's output entirely (the child still runs and gets exit-checked)"""
    # Inside a scheduled build phase a capture sink is active: route the child's output through console(),
    # so it lands in the owning display task and the log instead of tearing the live region. Outside it
    # keep stdio direct: the child needs the real terminal for prompts, and there is no sink anyway.
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
    """Wrapper around SubProcess.run(), returns (exit_status, output_string).
    cwd: working dir for the subprocess
    cmd: command string
    echo: if True, also prints the output to console
    env: overrides the environment for the subprocess, default is os.environ
    out: optional `(line) -> None` sink. When set, lines go there instead of the console"""
    lines = []  # list + join, NOT output += line: the latter is O(n^2) over a big build's output
    def handle_output(p:SubProcess, line:str):
        if out:    out(line)
        elif echo: console(line)  # NOT print: a raw write tears the live region's cursor math
        lines.append(line)
    try:
        exit_status = SubProcess.run(cmd, cwd, env=env, io_func=handle_output)
        return (exit_status, '\n'.join(lines))
    except Exception as e:
        lines.append(str(e))
        return (-1, '\n'.join(lines))
