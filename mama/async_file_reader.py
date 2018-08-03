import os, threading, fcntl
from queue import Queue
from time import sleep

class AsyncFileReader:
    def __init__(self, f):
        self.f = f
        self.queue = Queue()
        self.thread = threading.Thread(target=self._read_thread)
        self.keep_polling = True
        self.thread.daemon = True
        self.thread.start()
    
    def _read_thread(self):
        fd = self.f.fileno()
        flag = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, flag | os.O_NONBLOCK)
        while self.keep_polling and not self.f.closed:
            while True:
                line = self.f.readline()
                if not line: break
                self.queue.put(line)
            sleep(0.015)

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
        self.print()

