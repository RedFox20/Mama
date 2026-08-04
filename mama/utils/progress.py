"""Human-readable sizes and times, the transfer progress bar, and the git progress-line reader."""

import re, time

from .system import progress


def get_file_size_str(size):
    """Returns the file size as a human readable string, eg 96.5KB or 100.1MB."""
    if size < 128: return f'{size}B' # only show bytes for really small < 0.1 KB sizes
    if size < (1024*1024): return f'{size/1024:.1f}KB'
    if size < (1024*1024*1024): return f'{size/(1024*1024):.1f}MB'
    return f'{size/(1024*1024):.2}GB'


def get_time_str(seconds: float):
    if seconds < 0.1: return f'{int(seconds*1000)}ms'  # ms only below 0.1s, because 0.2s reads better than 200ms
    if seconds < 60: return f'{seconds:.1f}s'
    if seconds < 60*60: return f'{int(seconds/60)}m {int(seconds%60)}s'
    if seconds < 24*60*60: return f'{int(seconds/(60*60))}h {int(seconds/60)%60}m {int(seconds)%60}s'
    return f'{int(seconds/(24*60*60))}d {int((seconds%(24*60*60))/(60*60))}h {int(seconds/60)%60}m {int(seconds)%60}s'


class ProgressBar:
    """In-place `|   <====| NN% (time)` bar: drawn on construction, committed by finish(). Redraws
    throttle by payload size (100MB every 1%, under ~1MB none) so small payloads never flicker."""
    def __init__(self, total: int, indent: str = '    '):
        self.total = total
        self.indent = indent
        self.interval = max(1, int((100*1024*1024) / total)) if total else 100
        self.start = time.time()
        self.done = 0
        self.percent = 0
        self.label = ''
        self._draw(0)  # via progress(), so a headless run throttles the opening bar like every redraw

    def _percent(self) -> int:
        return int((self.done / self.total) * 100.0) if self.total else 100

    def _tail(self) -> str:
        """Current item, truncated from the left so the informative filename tail survives."""
        if not self.label: return ''
        return f' {self.label}' if len(self.label) <= 32 else f' ...{self.label[-29:]}'

    def _draw(self, percent: int, final=False):
        n = int(percent / 2)
        bar = f'|{" "*(50-n)}<{"="*n}| {percent:>3}% ({get_time_str(time.time()-self.start)})'
        progress(f'{self.indent}{bar}{self._tail()}', final=final)

    def step(self, amount: int, label: str = ''):
        """Advances by `amount` bytes. `label` names the item in flight, shown on the next redraw."""
        self.done += amount
        self.label = label
        if self.interval >= 100: return
        percent = self._percent()
        if abs(self.percent - percent) < self.interval: return
        self.percent = percent
        self._draw(percent)

    def finish(self):
        """Commit the bar on its own line. Always drawn, even when redraws were throttled off, and
        reports the real percent so a truncated transfer is visible rather than claiming 100%."""
        self.label = ''  # at 100% there is no item in flight, so keep the committed line clean
        self._draw(self._percent(), final=True)


# git transfer progress ('Receiving objects: 42% (...)') classification - shared so every place that
# captures git output collapses the per-percent flood identically.
_GIT_PROGRESS = (('remote: Counting objects:', 'counting objects   '), ('remote: Compressing objects:', 'compressing objects'),
                 ('Receiving objects:', 'receiving objects  '), ('Resolving deltas:', 'resolving deltas   '),
                 ('Updating files:', 'updating files     '))


def git_progress_status(line: str):
    """(status label, percent) for a raw git transfer-progress line ('Receiving objects: 42%'), else None."""
    for needle, status in _GIT_PROGRESS:
        if needle in line:
            pct = line.split('%')[0].rsplit(':', 1)[-1].strip()
            return status, (int(pct) if pct.isdigit() else 0)
    return None


_PERCENT_RE = re.compile(r'\b\d{1,3}%')  # a NN% completion token: git 'Receiving objects: 42%', a
                                         # download bar '|===| 42%', mama's collapsed redraw, wget/curl, ...


def is_progress_line(line: str) -> bool:
    """True for any transfer/download progress update - a line carrying a NN% completion token.
    Consecutive such lines collapse to just the latest, so a progress bar (git, artifactory download,
    a custom build's own downloader) cannot flood a captured buffer with hundreds of per-percent updates."""
    return _PERCENT_RE.search(line) is not None
