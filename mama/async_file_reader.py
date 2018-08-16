import os, sys, threading
from queue import Queue
from time import sleep
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
            while self.keep_polling and not self.f.closed:
                while not self.f.closed:
                    line = self.f.readline()
                    if not line: break
                    self.queue.put(line)
                if not blocking:
                    sleep(0.015)
        except:
            return

    def available(self):
        return not self.queue.empty()

    def readline(self):
        if self.available():
            return self.queue.get()
        return ''

    def print(self):
        while self.available():
            print(self.readline(), flush=True, end='')

    def stop(self):
        self.keep_polling = False
        self.thread.join()
        # self.print()

