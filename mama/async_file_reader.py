import os, sys, threading
from queue import Queue
from collections import deque
from time import sleep, time
if sys.platform.startswith('linux'):
    def set_nonblocking(fd):
        import fcntl
        flag = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, flag | os.O_NONBLOCK)
else:
    def set_nonblocking(fd):
        import msvcrt
        from ctypes import windll, byref, wintypes, WinError, POINTER
        from ctypes.wintypes import HANDLE, DWORD, BOOL
        PIPE_NOWAIT = DWORD(0x00000001)
        def pipe_no_wait(pipefd):
            SetNamedPipeHandleState = windll.kernel32.SetNamedPipeHandleState
            SetNamedPipeHandleState.argtypes = [HANDLE, POINTER(DWORD), POINTER(DWORD), POINTER(DWORD)]
            SetNamedPipeHandleState.restype = BOOL
            h = msvcrt.get_osfhandle(pipefd)
            res = windll.kernel32.SetNamedPipeHandleState(h, byref(PIPE_NOWAIT), None, None)
            if res == 0:
                print(WinError())
                return False
            return True
        return pipe_no_wait(fd)
    

class AsyncFileReader:
    def __init__(self, f):
        self.f = f
        self.queue = Queue()
        self.thread = threading.Thread(target=self._read_thread)
        self.keep_polling = True
        self.thread.daemon = True
        self.thread.start()
    
    def _read_thread(self):
        blocking = True
        if sys.platform.startswith('linux'):
            set_nonblocking(self.f.fileno())
            blocking = False
        try:
            f = self.f
            while self.keep_polling and not f.closed:
                while not f.closed:
                    line = f.readline()
                    if not line: break
                    self.queue.put( ( time(), line ) )
                if not blocking:
                    sleep(0.015)
        except:
            return

    def available(self):
        return not self.queue.empty()

    def readline(self):
        if self.available():
            return self.queue.get()[1]
        return ''

    def get(self):
        if self.available():
            return self.queue.get()
        return None

    def print(self):
        while self.available():
            print(self.readline(), flush=True, end='')

    def stop(self):
        self.keep_polling = False
        self.thread.join()
        # self.print()



class AsyncConsoleReader:
    def __init__(self, stdout, stderr):
        self.out = AsyncFileReader(stdout)
        self.err = AsyncFileReader(stderr)
        self.current_out = None
        self.current_err = None

    def available(self):
        return (self.current_out and self.current_err) or self.out.available() or self.err.available()

    def _peek_out(self):
        if self.current_out: return self.current_out
        self.current_out = self.out.get()
        return self.current_out

    def _peek_err(self):
        if self.current_err: return self.current_err
        self.current_err = self.err.get()
        return self.current_err

    def stop(self):
        self.out.stop()
        self.err.stop()

    def read(self):
        out = self._peek_out()
        err = self._peek_err()
        if out and (not err or out[0] <= err[0]):
            self.current_out = None
            return (out[1], None)
        if err and (not out or err[0] <= out[0]):
            self.current_err = None
            return (None, err[1])
        return (None, None)

