"""Pins that no mama code spawns a child with an inherited stderr, which no filter can reach."""
import os
import re

import pytest

_MAMA = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'mama')
_SPAWN = re.compile(r'subprocess\.(run|Popen|check_output|call|check_call)\(|os\.(system|popen)\(')
# Every raw spawn that is allowed, and why. sub_process.py IS the wrapper. ssh must stay silent,
# because ssh warns once per bad ssh_config line. The two git probes swallow an expected `fatal:`.
_ALLOWED = {'utils/sub_process.py', 'utils/ssh_multiplex.py', 'types/git.py', 'util.py'}


def _py_files():
    for root, _, files in os.walk(_MAMA):
        for f in files:
            if f.endswith('.py'): yield os.path.join(root, f)


@pytest.mark.parametrize('path', sorted(_py_files()))
def test_a_raw_spawn_lives_only_where_it_is_justified(path):
    rel = os.path.relpath(path, _MAMA).replace('\\', '/')
    if rel in _ALLOWED: return
    hits = [n for n, line in enumerate(open(path, encoding='utf-8'), 1) if _SPAWN.search(line)]
    assert not hits, (f'{rel}:{hits} spawns a child directly. Use mama.utils.sub_process '
                      '(SubProcess.run / execute_echo / execute_piped), or justify it in _ALLOWED.')


def test_every_allowed_spawn_captures_stderr():
    """os.system and a bare stdout=PIPE inherit stderr: the child writes past every mama filter and
    tears the live display, which redraws by counting the lines it wrote itself."""
    leaks = []
    for rel in sorted(_ALLOWED):
        lines = open(os.path.join(_MAMA, rel), encoding='utf-8').read().splitlines()
        for n, line in enumerate(lines, 1):
            if not _SPAWN.search(line): continue
            call = ' '.join(lines[n - 1:n + 1])  # keyword args often wrap onto the next line
            if 'stderr=' in call or 'capture_output' in call: continue
            leaks.append(f'{rel}: {line.strip()}')
    # Two stay by design: the no-capture Popen (io_func=None hands the child the real terminal) and
    # execute(), the interactive launcher for `code` / `open` / a prompting apt-get. Pinned by code,
    # not by line number, so an edit above them does not fail this test.
    assert leaks == ['utils/sub_process.py: self.process = subprocess.Popen(args, cwd=cwd, env=env)',
                     'utils/sub_process.py: retcode = os.system(command)']
