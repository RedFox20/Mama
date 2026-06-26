import os, shlex, shutil, threading, queue
import subprocess
from .system import System, console, error


# Linux/macOS: we allocate a PTY for the child so git etc. still see a TTY
# (preserves progress output and isatty checks). The pty.openpty() syscall
# does NOT fork - it just creates a master/slave fd pair - so it's safe to
# call from a worker thread. subprocess.Popen does the actual fork via
# posix_spawn/vfork which is multi-thread-safe, unlike the older os.forkpty
# which Python 3.12 flags with a DeprecationWarning specifically because of
# the threaded-deadlock risk.
if not System.windows:
    import pty
    import select


READER_IDLE_TIMEOUT = 0.1  # seconds; how long to wait before flushing a \r-progress partial
READER_CHUNK = 8192


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
        else:
            # Allocate a PTY pair; child gets the slave end as its stdin/stdout/stderr.
            self._master_fd, slave = pty.openpty()
            try:
                self.process = subprocess.Popen(args, cwd=cwd, env=env,
                                                stdin=slave, stdout=slave, stderr=slave, close_fds=True)
            finally:
                os.close(slave) # parent doesn't need the slave once Popen has it

        self._reader_thread = threading.Thread(target=self._read_loop, daemon=True)
        self._reader_thread.start()


    def _read_loop(self):
        try:
            if self._master_fd is not None:
                self._read_loop_pty()
            else:
                self._read_loop_pipe()
        except Exception as e:
            # Capture so run() can surface it. Don't crash the reader thread.
            self._reader_exc = e


    def _read_loop_pty(self):
        """Drain PTY master with select-based idle detection so \\r-terminated
        progress (ninja/cmake/git) surfaces without waiting for a \\n."""
        buf = bytearray()
        while True:
            ready, _, _ = select.select([self._master_fd], [], [], READER_IDLE_TIMEOUT)
            if ready:
                try: chunk = os.read(self._master_fd, READER_CHUNK)
                except OSError: break  # EIO on a closed slave end
                if not chunk: break
                buf.extend(chunk)
                self._drain_buffer(buf)
            else:
                self._drain_buffer(buf, idle=True)
        self._drain_buffer(buf, eof=True)


    def _read_loop_pipe(self):
        """Windows path: select() doesn't work on pipes, so a helper thread does the blocking
        byte reads and hands chunks to a queue. The drain loop waits on the queue with
        READER_IDLE_TIMEOUT, giving the same \\r-progress idle-flush as the PTY path. Without it
        a \\r at a chunk boundary hangs until the next read, and a CRLF after it (the CRT turns
        the child's \\n into \\r\\n) surfaces as a spurious empty line."""
        stdout = self.process.stdout
        if not stdout: return
        fd = stdout.fileno()
        chunks: queue.Queue = queue.Queue()
        def pump():
            while True:
                try: chunk = os.read(fd, READER_CHUNK)
                except OSError: chunk = b''
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
                    self._emit_io_out(buf[pos:cr]);
                    pos = cr + 1;
                    continue
                if cr + 1 == n and (idle or eof):
                    self._emit_io_out(buf[pos:cr]);
                    pos = cr + 1;
                    self._swallow_lf = True;
                    continue
            nl = buf.find(b'\n', pos)
            if nl >= 0:
                end = nl - 1 if nl > pos and buf[nl - 1] == 0x0d else nl
                self._emit_io_out(buf[pos:end]);
                pos = nl + 1;
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


    def kill(self):
        if self.process and self.process.poll() is None:
            try:
                self.process.terminate()
                self.process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                try:
                    self.process.kill()
                    self.process.wait(timeout=1.0)
                except Exception:
                    pass
            except Exception:
                pass


    def close(self):
        self.kill()
        # Reader thread exits when the PTY master sees EOF (slave closed by
        # the child or by Popen's __exit__). Join briefly to drain any
        # trailing buffered output the io_func hasn't seen yet.
        if self._reader_thread:
            self._reader_thread.join(timeout=2.0)
            self._reader_thread = None
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


    @staticmethod
    def run(cmd, cwd=None, env=None, io_func=None, timeout=None):
        """
        Runs `cmd` and returns its exit status.
        - cmd:     command string (shlex.split) or list of args.
        - cwd:     working directory for the child.
        - env:     environment dict, defaults to os.environ.
        - io_func: callback `(SubProcess, line:str)` for each output line;
                   if None, child inherits parent's std streams.
        - timeout: kill the child after this many seconds (raises
                   subprocess.TimeoutExpired). Default: no timeout.
        """
        p = SubProcess(cmd, cwd=cwd, env=env, io_func=io_func)
        try:
            try:
                p.status = p.process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                p.kill()
                raise
        finally:
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


def execute_echo(cwd, cmd, exit_on_fail=False, env=None):
    """
    Wrapper around SubProcess.run(), by default throws if exit_status != 0
    - cwd: working dir for the subprocess
    - cmd: command string
    - exit_on_fail: if True, exits the application with exit_status
    - env: overrrides the environment for the subprocess, default is os.environ
    """
    exit_status = -1
    throw_on_fail = not exit_on_fail
    try:
        exit_status = SubProcess.run(cmd, cwd, env=env, io_func=None)
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
    try:
        exit_status = -1
        output = ''
        def handle_output(p:SubProcess, line:str):
            nonlocal output
            if out:    out(line)
            elif echo: print(line)
            output += line
            output += '\n' # newline is not included
        exit_status = SubProcess.run(cmd, cwd, env=env, io_func=handle_output)
        return (exit_status, output)
    except Exception as e:
        return (-1, f'{output}{e}')
