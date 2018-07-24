import threading
from queue import Queue

class AsyncFileReader:
    def __init__(self, file):
        self.file = file
        self.queue = Queue()
        self.thread = threading.Thread(target=self._read_thread)
        self.keep_polling = True
        self.thread.daemon = True
        self.thread.start()
    
    def _read_thread(self):
        while self.keep_polling:
            self.queue.put(self.file.readline())

    def readline(self):
        if self.queue.empty():
            return ''
        return self.queue.get()

    def stop(self):
        self.keep_polling = False
        self.thread.join()
