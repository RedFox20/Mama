"""Measure CMake CONFIGURE overhead (no compilation) across real repos, to judge config-speed
ideas on data. For each repo in repos.txt it reports three numbers with the VS generator:

    cold   - fresh build dir (full compiler detection)
    seeded - mama's ABI seed injected (skips the ABI try_compile)
    warm   - configure twice, 2nd run (detection cached) = the pre-warm ceiling

The cold->warm gap is the headroom a parallel detection pre-warm could capture. This is a manual
benchmark (clones + runs real cmake); it is NOT part of the unit suite.

    python bench/bench_config_overhead.py            # all repos in repos.txt
    python bench/bench_config_overhead.py ReCpp      # just one
"""
import os, re, sys, time, shutil, subprocess, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
from mama import cmake_compiler_cache as cc  # noqa: E402

REPOS_DIR = os.path.join(HERE, 'repos')
_DETECT = re.compile(r'identification is|compiler ABI info|compile features')


def sh(args, cwd=None):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True)


def vs_generator():
    out = sh(['cmake', '--help']).stdout
    vs = [l.split('=')[0].strip().lstrip('* ') for l in out.splitlines() if 'Visual Studio' in l and '=' in l]
    return vs[0] if vs else ''


def cmake_ver():
    out = sh(['cmake', '--version']).stdout
    m = re.search(r'(\d+\.\d+\.\d+)', out)
    return m.group(1) if m else 'unknown'


def configure(src, build, gen, extra=()):
    args = ['cmake']
    if gen: args += ['-G', gen, '-A', 'x64']
    args += [*extra, '-S', src, '-B', build]
    t = time.monotonic()
    cp = sh(args)
    return time.monotonic() - t, (_DETECT.findall(cp.stdout).__len__()), cp.returncode == 0


def fresh(prefix):
    d = tempfile.mkdtemp(prefix=prefix)
    return d


def bench_repo(name, src, gen, ver):
    cf = lambda b: os.path.join(b, 'CMakeFiles', ver)
    # cold
    b1 = fresh(f'{name}_cold_'); cold, dcold, ok1 = configure(src, b1, gen)
    # warm (2nd configure of same dir)
    configure(src, b1, gen); warm, dwarm, _ = configure(src, b1, gen)
    # seeded: publish from the cold build, inject the warm state into a fresh dir
    seed = fresh(f'{name}_seed_'); cc.publish(seed, cf(b1))
    b2 = fresh(f'{name}_seeded_'); cc.inject(seed, b2, cf(b2), src)
    seeded, dseed, ok2 = configure(src, b2, gen)
    for d in (b1, b2, seed): shutil.rmtree(d, ignore_errors=True)
    return cold, seeded, warm, dcold, dseed, dwarm, ok1 and ok2


def clone_missing(only):
    os.makedirs(REPOS_DIR, exist_ok=True)
    repos = []
    for line in open(os.path.join(HERE, 'repos.txt'), encoding='utf-8'):
        line = line.strip()
        if not line or line.startswith('#'): continue
        parts = line.split()
        name, url = parts[0], parts[1]
        if only and name not in only: continue
        branch = parts[2] if len(parts) > 2 else None
        subdir = parts[3] if len(parts) > 3 else ''
        dest = os.path.join(REPOS_DIR, name)
        if not os.path.isdir(dest):
            args = ['git', 'clone', '--depth=1', '--recurse-submodules']
            if branch: args += ['--branch', branch]
            print(f'cloning {name} ...')
            if sh(args + [url, dest]).returncode != 0:
                print(f'  skip {name} (clone failed - private/auth?)'); continue
        repos.append((name, os.path.join(dest, subdir) if subdir else dest))
    return repos


def main():
    only = set(sys.argv[1:])
    gen, ver = vs_generator(), cmake_ver()
    print(f'generator: {gen or "(default)"}   cmake: {ver}\n')
    print(f'{"repo":<12}{"cold":>7}{"seeded":>8}{"warm":>7}   {"cold->seeded":>13}{"cold->warm":>12}   detect c/s/w')
    for name, src in clone_missing(only):
        if not os.path.exists(os.path.join(src, 'CMakeLists.txt')):
            print(f'{name:<12} no CMakeLists.txt at {src}'); continue
        cold, seeded, warm, dc, ds, dw, ok = bench_repo(name, src, gen, ver)
        pct = lambda a, b: f'{(1 - b / a) * 100:4.0f}%' if a else '   -'
        print(f'{name:<12}{cold:6.1f}s{seeded:7.1f}s{warm:6.1f}s   {pct(cold, seeded):>13}{pct(cold, warm):>12}'
              f'   {dc}/{ds}/{dw}{"" if ok else "  (configure FAILED)"}')


if __name__ == '__main__':
    main()
