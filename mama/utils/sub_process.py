import os, shlex, shutil
from signal import SIGTERM
from errno import ECHILD
import subprocess
from time import sleep
from .nonblocking_io import set_nonblocking
from .system import System, console, error


class SubProcess:
    """
    An alternative to subprocess.Popen with redirectable IO
    using fork and forktty on UNIX.

    Windows version uses standard subprocess.Popen with pipes

    Any redirected stdout/stderr which needs to retain its
    terminal colors etc, should use this SubProcess
    """
    def __init__(self, cmd, cwd, env=None, io_func=None):
        self.io_func = io_func

        env = env if env else os.environ.copy()
        args = shlex.split(cmd)

        executable = args[0]
        if os.path.isfile(executable): # it's something like `./run_tests` or `/usr/bin/gcc`
            executable = os.path.abspath(executable)
        else: # lookup from PATH
            executable = shutil.which(args[0])
            if not executable:
                raise OSError(f"SubProcessed failed to start: {args[0]} not found in PATH")
        args[0] = executable

        if System.windows:
            self.process = None
            
            try:
                self.process = subprocess.Popen(args, cwd=cwd, env=env, shell=True,
                                                universal_newlines=True,
                                                stdin=subprocess.PIPE,
                                                stdout=subprocess.PIPE,
                                                stderr=subprocess.STDOUT)
                set_nonblocking(self.process.stdout.fileno())
            except Exception as e:
                raise Exception(f"Popen failed {args}: {e}")
        else: # all UNIX based systems support fork or forkpty
            # FD visible only for the parent process, 
            # and can be used to read the child PTY output
            self.parent_fd = 0
            self.status = -1

            if io_func:
                self.pid, self.parent_fd = os.forkpty()
            else:
                self.pid = os.fork()

            # 0: inside the child process, PID inside the parent process
            if self.pid == -1:
                raise OSError(f"SubProcess failed to start: {cmd}")
            elif self.pid == 0: # child process:
                if cwd: os.chdir(cwd)
                # execve: universal, but requires full path to program
                os.execve(executable, args, env)
            else: # parent process:
                # set the parent FD as non-blocking, otherwise the async tasks will never finish
                set_nonblocking(self.parent_fd)


    def close(self):
        self.kill()
        if System.windows:
            self.process.wait(1.0)
            self.process = None
        else:
            if self.parent_fd:
                os.close(self.parent_fd)
                self.parent_fd = 0


    def kill(self):
        if System.windows:
            self.process.kill()
        else:
            pid, self.pid = (self.pid, 0)
            if pid > 0:
                try:
                    os.kill(pid, SIGTERM)
                except:
                    pass


    def try_wait(self):
        """ Returns EXIT_STATUS int if process has finished, otherwise None """
        if System.windows:
            return self.process.poll()
        else:
            try:
                r, status = os.waitpid(self.pid, os.WNOHANG)
                if r == self.pid: # r == pid: process finished
                    return self.handle_exitstatus(status)
            except OSError as e:
                if e.errno == ECHILD:
                    return -1 # ECHILD: no such child
            return None


    def handle_exitstatus(self, status):
        if os.WIFSIGNALED(status):
            self.status = -os.WTERMSIG(status)
        elif os.WIFEXITED(status):
            self.status = os.WEXITSTATUS(status)
        return -1


    def parse_lines(self, text: str):
        end = len(text)
        start = 0
        line = ''
        while start < end:
            current = text.find('\n', start)
            if current != -1:
                eol = current
                if (eol-start) > 0 and text[eol-1] == '\r':
                    eol -= 1 # drop the '\r'
                line = text[start:eol]
                start = current + 1
            else: # last token:
                line = text[start:]
                start = end
            self.io_func(line)


    def read_output(self):
        """ 
        Returns TRUE if output was read.
        Calls self.io_func(line) for every line that was read.
        Newlines are INCLUDED.
        """
        try:
            bytes = None
            if System.windows:
                if not self.process:
                    return False
                console('read start')
                bytes = self.process.stdout.read()
                console('read finished')
            else:
                if not self.parent_fd:
                    return False
                bytes = os.read(self.parent_fd, 8192)

            got_bytes = len(bytes) > 0
            if self.io_func and got_bytes:
                text = bytes.decode()
                self.parse_lines(text)
            return got_bytes
        except OSError as e:
            # when in non-blocking IO, EAGAIN will be thrown if there's no data
            # and when the other process closes the pipes
            console(f'OSError: {e}')
            return False


    @staticmethod
    def run(cmd, cwd=None, env=None, io_func=None):
        """
        Runs the titled sub-process with `cmd` using fork or forktty if io_func is set
        - cmd: full command string
        - cwd: working dir for the subprocess
        - env: execution environment, or None for default env
        - io_func: if set, this callback will receive each line from output
                   if None, then no output will be shown

        ```
        SubProcess.run('tool', 'cmake xyz', env)
        SubProcess.run('tool', 'cmake xyz', io_func=lambda line: print(line))
        ```
        """
        p = SubProcess(cmd, cwd, env, io_func=io_func)
        try:
            while not p.try_wait():
                p.read_output()
                sleep(0.01)
            p.read_output() # read any trailing output
        finally:
            p.close()
        return p.status


def execute(command, echo=False, throw=True):
    if echo: console(command)
    retcode = os.system(command)
    if throw and retcode != 0:
        raise Exception(f'{command} failed with return code {retcode}')
    return retcode


def execute_piped(command, cwd=None, timeout=None):
    if not isinstance(command, list):
        command = shlex.split(command)
    try:
        cp = subprocess.run(command, stdout=subprocess.PIPE, cwd=cwd, timeout=timeout)
        return cp.stdout.decode('utf-8').rstrip()
    except Exception as e:
        raise Exception(f'subprocess.Run {command} failed: {e}')


def execute_echo(cwd, cmd):
    """ Wrapper around SubProcess.run(), throws if exit_status != 0 """
    exit_status = -1
    try:
        exit_status = SubProcess.run(cmd, cwd)
    except:
        error(f'SubProcess failed! cwd={cwd} cmd={cmd} ')
        raise
    if exit_status != 0:
        raise Exception(f'Execute {cmd} failed with error: {exit_status}')


def execute_piped_echo(cwd, cmd, echo=True):
    """ Wrapper around SubProcess.run(), returns status code with piped output (status, output). """
    try:
        exit_status = -1
        output = ''
        def handle_output(line:str):
            nonlocal output
            if echo: print(line) # newline is not included
            output += line
            output += '\n'
        exit_status = SubProcess.run(cmd, cwd, io_func=handle_output)
        return (exit_status, output)
    except Exception as e:
        return (-1, f'{output}{e}')