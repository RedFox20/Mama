"""Profile mama itself: the child processes a run spawns, and where its own Python time goes.

Three modes, in the order to use them:

    python bench/profile_mama.py census pytest tests/test_git_pin_change/
    python bench/profile_mama.py census mama build
        Count every child process the run spawns, with total and average wall time. mama is IO bound,
        so this answers "why is it slow" more often than a profiler does. A row whose average passes
        --slow (0.5 seconds by default) carries a SLOW mark. Ask two questions about a marked row.
        Does the run need that call at all, and does it need it that many times.

    python bench/profile_mama.py sample pytest tests/test_git_pin_change/
        py-spy flamegraph of every thread, written to bench/_out. The -wall file counts blocked
        threads, so a wide plateau there is a wait. The -cpu file counts running threads only.
        What is wide in BOTH is real work. Needs `pip install py-spy`.

    python bench/profile_mama.py tests
        The slowest tests, from pytest --durations.

Read the census first. mama burns about 3 seconds of Python CPU in a 50 second build, so a profiler
alone points at threading.wait and teaches nothing. Two findings came out of the census this way.
`git clone --recurse-submodules` cost 1 second on a repository with no submodule, and the compiler
seed probe cost 4 seconds once per workspace.
"""
import collections, os, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(HERE, '_out')
sys.path.insert(0, ROOT)

SLOW_SECONDS = 0.5  # a single child call over this is worth a look, see the module docstring
_stats = collections.defaultdict(lambda: [0, 0.0])  # command -> [count, seconds]


def command_key(args) -> str:
    """Name the command family a census row counts, eg `git fetch` or `cmake --build`. cmake gets its
    own split, because a configure, a build and the seed probe cost wildly different amounts."""
    parts = [str(p) for p in (args if isinstance(args, (list, tuple)) else str(args).split())]
    if not parts: return '<empty>'
    exe = os.path.basename(parts[0]).lower().removesuffix('.exe')
    if exe == 'cmake':
        joined = ' '.join(parts)
        if 'mama_seed_' in joined: return 'cmake SEED PROBE'
        if '--build' in parts:     return 'cmake --build'
        if '--version' in parts:   return 'cmake --version'
        return 'cmake configure'
    verb = next((p for p in parts[1:] if not p.startswith('-')), '')
    return f'{exe} {verb}'.strip()


def install_census():
    """Time every child process by wrapping subprocess.Popen. One hook catches SubProcess.run,
    execute_piped and any direct subprocess.run, because all three end up in Popen."""
    orig_init, orig_wait = subprocess.Popen.__init__, subprocess.Popen.wait

    def init(self, args, *a, **kw):
        self._census = [command_key(args), time.perf_counter(), False]
        orig_init(self, args, *a, **kw)

    def wait(self, timeout=None):
        rc = orig_wait(self, timeout)
        census = getattr(self, '_census', None)
        if census and not census[2]:  # the first wait that returns owns the timing
            census[2] = True
            entry = _stats[census[0]]
            entry[0] += 1
            entry[1] += time.perf_counter() - census[1]
        return rc

    subprocess.Popen.__init__, subprocess.Popen.wait = init, wait


def report_census(elapsed: float, slow: float):
    spawns = sum(n for n, _ in _stats.values())
    child = sum(s for _, s in _stats.values())
    print(f'\n== child process census: {spawns} spawns, {child:.1f}s of child time in a {elapsed:.1f}s run ==')
    print(f'{"count":>6} {"total_s":>9} {"avg_ms":>8}  command')
    for key, (n, s) in sorted(_stats.items(), key=lambda kv: -kv[1][1]):
        mark = '  <-- SLOW' if s / n >= slow else ''
        print(f'{n:>6} {s:>9.2f} {1000*s/n:>8.0f}  {key}{mark}')
    print(f'\nmama itself used {max(0.0, elapsed - child):.1f}s, the rest waited on the children above.')


def run_target(argv: list) -> int:
    """Run pytest or mama IN THIS PROCESS, so the census hook sees every child they spawn."""
    tool, rest = argv[0], argv[1:]
    if tool == 'pytest':
        import pytest
        return pytest.main(rest)
    if tool == 'mama':
        import mama
        mama.mamabuild(rest, source_dir=os.getcwd())
        return 0
    raise SystemExit(f'census takes `pytest ...` or `mama ...`, not {tool!r}')


def sample(argv: list):
    """Record a wall-clock and a CPU flamegraph with py-spy. Both cover every thread."""
    os.makedirs(OUT, exist_ok=True)
    cmd = [sys.executable, '-m', 'pytest', *argv[1:]] if argv[0] == 'pytest' else [sys.executable, '-m', 'mama.main', *argv[1:]]
    for name, extra in (('wall', ['--idle']), ('cpu', [])):
        out = os.path.join(OUT, f'profile-{name}.svg')
        print(f'\n== py-spy {name} -> {out}')
        rc = subprocess.run(['py-spy', 'record', '--threads', '--rate', '200', *extra, '-o', out, '--', *cmd]).returncode
        if rc != 0: raise SystemExit('py-spy failed. Install it with `pip install py-spy`.')


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('--slow')]
    slow = next((float(a.split('=')[1]) for a in sys.argv[1:] if a.startswith('--slow=')), SLOW_SECONDS)
    mode = args[0] if args else ''
    if mode == 'census':
        install_census()
        start = time.perf_counter()
        try: run_target(args[1:])
        finally: report_census(time.perf_counter() - start, slow)
    elif mode == 'sample':
        sample(args[1:])
    elif mode == 'tests':
        subprocess.run([sys.executable, '-m', 'pytest', 'tests/', '-q', '--durations=40', '--durations-min=0.2'], cwd=ROOT)
    else:
        print(__doc__)


if __name__ == '__main__':
    main()
