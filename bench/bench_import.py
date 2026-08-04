"""Measure what `import mama` costs, and name what it loads that it should not.

    python bench/bench_import.py
        The median of N cold interpreter runs, plus the deferred-module check.

    python bench/bench_import.py --tree
        The 25 costliest modules of one run, self time first. Use this to find the next offender.

Every mama process pays this cost once, and a run that bootstraps a host binary or shells out to
`mama <host> build` pays it again per child.

Two rules keep it low, and both rot silently without this script:

1. No module-level import of a package that only a rare code path needs. `psutil`, `ssl`,
   `zipfile`, `ftplib` and `dateutil` cost 20ms or more each.
2. Nothing calls `platform.machine()` at import. Python 3.12 and later answer `platform.uname()`
   on Windows through a WMI query, which costs about 60ms.

`tests/test_import_cost/` pins rule 1 and rule 2 in CI. This script says what the cost IS.
"""
import argparse
import json
import statistics
import subprocess
import sys

# Import cost measured on Windows, in ms: psutil 32, zipfile 28, ssl 26, dateutil.tz 25, ftplib 21.
DEFERRED = ['psutil', 'ssl', '_ssl', 'zipfile', 'ftplib', 'dateutil.tz', 'urllib.request']
BUDGET_MS = 110.0  # measured at about 93ms. Fails over this, so a regression is loud


def _run(code: str) -> str:
    out = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, timeout=180)
    if out.returncode != 0:
        raise SystemExit(f'child failed:\n{out.stderr}')
    return out.stdout.strip()


def measure(runs: int) -> list:
    """Wall time of `import mama` in a COLD interpreter, one process per sample. A warm process
    would answer from sys.modules and report nothing."""
    code = 'import time; t=time.perf_counter(); import mama; print((time.perf_counter()-t)*1000)'
    return [float(_run(code).splitlines()[-1]) for _ in range(runs)]


def leaked_modules() -> list:
    """The deferred modules that `import mama` loaded anyway."""
    code = f'import sys, json; import mama; print(json.dumps([m for m in {DEFERRED!r} if m in sys.modules]))'
    return json.loads(_run(code).splitlines()[-1])


def print_tree(limit: int):
    out = subprocess.run([sys.executable, '-X', 'importtime', '-c', 'import mama'],
                         capture_output=True, text=True, timeout=180)
    rows = []
    for line in out.stderr.splitlines():
        parts = line.split('|')
        if len(parts) == 3 and 'self' not in parts[0]:
            try: rows.append((int(parts[0].split(':')[1]), int(parts[1]), parts[2].strip()))
            except ValueError: pass
    rows.sort(reverse=True)
    print(f'\n{"self us":>10} {"cumulative us":>14}  module')
    for self_us, cumulative, name in rows[:limit]:
        print(f'{self_us:>10} {cumulative:>14}  {name}')


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--runs', type=int, default=7, help='cold interpreter samples (default 7)')
    parser.add_argument('--tree', action='store_true', help='show the costliest modules of one run')
    parser.add_argument('--budget', type=float, default=BUDGET_MS, help=f'fail over this (default {BUDGET_MS})')
    args = parser.parse_args()

    times = measure(args.runs)
    median = statistics.median(times)
    print(f'import mama: {median:.1f} ms median, {min(times):.1f} ms min, {max(times):.1f} ms max '
          f'({args.runs} cold runs)')

    leaked = leaked_modules()
    if leaked:
        print(f'\nFAIL: these should load only when a rare path needs them: {", ".join(leaked)}')
        print('Find the module-level import that pulls each one, and move it into its call site.')
    else:
        print(f'deferred modules: none loaded ({len(DEFERRED)} checked)')

    if args.tree: print_tree(25)

    if median > args.budget:
        print(f'\nFAIL: {median:.1f} ms is over the {args.budget:.0f} ms budget.')
        print('Run again with --tree to see which module grew.')
    return 1 if (leaked or median > args.budget) else 0


if __name__ == '__main__':
    sys.exit(main())
