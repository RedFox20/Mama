"""Per-build subprocess-tree CPU% sampling, isolated for unit-testing + benchmarking.

A sampler is `callable(snapshot) -> {tid: cpu%}`, where snapshot is `{tid: set(root_pids)}`. It sums
the cpu-time delta since the last sample over wall-clock for each build's OWN process tree (cmake ->
ninja/make/msbuild -> compilers), so a tree saturating N cores reads ~N*100%. It reads CPU only for
those trees, never every system process. Windows uses kernel32 directly (one CreateToolhelp32Snapshot
+ GetProcessTimes); other platforms use psutil. make_sampler() picks the right one (None if neither)."""
import time
from .system import System


def make_sampler():
    if System.windows:
        try: return WinTreeCpu()        # low-level kernel32: far cheaper than psutil on Windows
        except Exception: pass           # ctypes oddity -> fall back to psutil
    try: import psutil
    except ImportError: return None
    return PsutilTreeCpu(psutil)


def accumulate_cpu(state: dict, now: float, trees: dict) -> dict:
    """trees: {tid: {pid: (cpu_seconds, create_ts)}} -> {tid: cpu%}. Per pid, CPU% is the cpu-time
    delta since the last sample over wall-clock (first sight averages over the lifetime so far), so a
    build tree saturating N cores reads ~N*100%. `state` (pid -> (cpu_seconds, ts)) carries the delta
    across samples and is pruned of pids no longer in any tree."""
    result, seen = {}, set()
    for tid, procs in trees.items():
        total = 0.0
        for pid, (cur, create) in procs.items():
            seen.add(pid)
            base_cpu, base_ts = state.get(pid, (0.0, create))
            state[pid] = (cur, now)
            dt = now - base_ts
            if dt > 0: total += max(0.0, (cur - base_cpu) / dt * 100.0)
        result[tid] = total
    for pid in [p for p in state if p not in seen]: del state[pid]  # drop dead procs
    return result


class PsutilTreeCpu:
    """Process-tree CPU% via psutil (non-Windows): cpu_times read ONLY for each build's own tree, never
    every system process. oneshot() batches the per-proc cpu_times + create_time into one read."""
    def __init__(self, psutil):
        self._ps = psutil
        self._state: dict = {}  # pid -> (cpu_seconds, wallclock_ts)

    def __call__(self, snapshot) -> dict:
        ps, trees = self._ps, {}
        for tid, roots in snapshot.items():
            procs = {}
            for rp in roots:
                try:
                    root = ps.Process(rp)
                    tree = [root] + root.children(recursive=True)
                except ps.Error: continue
                for proc in tree:
                    try:
                        with proc.oneshot(): procs[proc.pid] = (sum(proc.cpu_times()[:2]), proc.create_time())
                    except ps.Error: continue
            trees[tid] = procs
        return accumulate_cpu(self._state, time.time(), trees)


class WinTreeCpu:
    """Process-tree CPU% straight from kernel32 (no psutil): ONE CreateToolhelp32Snapshot builds the
    ppid tree of all processes, then GetProcessTimes reads CPU for each build-tree pid. psutil's
    children(recursive=True) snapshots the whole process table once PER build root, plus heavy
    per-process handle work - that is what made Windows sampling slow."""
    _SNAPPROCESS = 0x00000002
    _QUERY = 0x1000  # PROCESS_QUERY_LIMITED_INFORMATION

    def __init__(self):
        import ctypes
        from ctypes import wintypes
        DW, H, BOOL, PTR = wintypes.DWORD, wintypes.HANDLE, wintypes.BOOL, ctypes.POINTER
        class PE32(ctypes.Structure):
            _fields_ = [('dwSize', DW), ('cntUsage', DW), ('th32ProcessID', DW),
                        ('th32DefaultHeapID', PTR(ctypes.c_ulong)), ('th32ModuleID', DW), ('cntThreads', DW),
                        ('th32ParentProcessID', DW), ('pcPriClassBase', ctypes.c_long), ('dwFlags', DW),
                        ('szExeFile', ctypes.c_char * 260)]
        class FT(ctypes.Structure):
            _fields_ = [('lo', DW), ('hi', DW)]
        k = ctypes.WinDLL('kernel32', use_last_error=True)
        k.CreateToolhelp32Snapshot.restype, k.CreateToolhelp32Snapshot.argtypes = H, [DW, DW]
        k.OpenProcess.restype, k.OpenProcess.argtypes = H, [DW, BOOL, DW]
        k.CloseHandle.argtypes = [H]
        k.Process32First.argtypes = k.Process32Next.argtypes = [H, PTR(PE32)]
        k.GetProcessTimes.argtypes = [H] + [PTR(FT)] * 4
        self._ct, self._k, self._PE32, self._FT = ctypes, k, PE32, FT
        self._invalid = ctypes.c_void_p(-1).value
        self._state: dict = {}  # pid -> (cpu_seconds, wallclock_ts)

    def _ppid_map(self) -> dict:
        ct, k = self._ct, self._k
        snap = k.CreateToolhelp32Snapshot(self._SNAPPROCESS, 0)
        if not snap or snap == self._invalid: return {}
        try:
            e = self._PE32(); e.dwSize = ct.sizeof(self._PE32); out = {}
            ok = k.Process32First(snap, ct.byref(e))
            while ok:
                out[e.th32ProcessID] = e.th32ParentProcessID
                ok = k.Process32Next(snap, ct.byref(e))
            return out
        finally:
            k.CloseHandle(snap)

    def _proc_times(self, pid):
        ct, k = self._ct, self._k
        h = k.OpenProcess(self._QUERY, False, pid)
        if not h: return None
        try:
            c, x, kern, usr = self._FT(), self._FT(), self._FT(), self._FT()
            if not k.GetProcessTimes(h, ct.byref(c), ct.byref(x), ct.byref(kern), ct.byref(usr)):
                return None
            tick = lambda f: (f.hi << 32) | f.lo  # 100ns units
            return (tick(kern) + tick(usr)) / 1e7, (tick(c) - 116444736000000000) / 1e7  # cpu_s, create_unix
        finally:
            k.CloseHandle(h)

    def __call__(self, snapshot) -> dict:
        ppids = self._ppid_map()
        kids: dict = {}
        for pid, ppid in ppids.items(): kids.setdefault(ppid, []).append(pid)
        trees = {}
        for tid, roots in snapshot.items():
            tree, stack = set(), [p for p in roots if p in ppids]
            while stack:
                pid = stack.pop()
                if pid in tree: continue
                tree.add(pid); stack.extend(kids.get(pid, ()))
            procs = {}
            for pid in tree:
                t = self._proc_times(pid)
                if t is not None: procs[pid] = t
            trees[tid] = procs
        return accumulate_cpu(self._state, time.time(), trees)
