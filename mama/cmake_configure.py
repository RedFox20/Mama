import os, subprocess, shlex
from mama.system import System, console
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


def rerunnable_cmake_conf(dependency, args, allow_rerun):
    rerun = False
    error = ''
    proc = subprocess.Popen(args, shell=True, universal_newlines=True, cwd=dependency.build_dir, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    output = AsyncFileReader(proc.stdout)
    errors = AsyncFileReader(proc.stderr)
    while True:
        if proc.poll() is None:
            line = output.readline()
            if line: console(line.rstrip())

            error = errors.readline()
            if error:
                error = error.rstrip()
                console(error)
                # this happens every time MSVC compiler is updated. simple fix is to rerun cmake
                if System.windows:
                    rerun |= error.startswith('  is not a full path to an existing compiler tool.')
        else:
            output.stop()
            errors.stop()
            if proc.returncode == 0:
                break
            if rerun:
                return rerunnable_cmake_conf(dependency, args, False)
            raise Exception(f'CMake configure error: {error}')

def run_cmake_config(dependency, generator, cmake_flags):
    cmd = f'{generator} {cmake_flags} -DCMAKE_INSTALL_PREFIX="." . "{dependency.src_dir}"'
    args = ['cmake']
    args += shlex.split(cmd)
    rerunnable_cmake_conf(dependency, args, True)