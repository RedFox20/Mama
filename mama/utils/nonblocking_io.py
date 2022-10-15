import os, sys

IS_POSIX = 'linux' in sys.platform.lower() or 'darwin' in sys.platform.lower()

if IS_POSIX:
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
