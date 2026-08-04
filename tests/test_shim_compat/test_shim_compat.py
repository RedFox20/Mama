"""Pins mama/util.py as a compatibility shim: mamafiles in repos mama does not control import from it."""
import importlib
import subprocess
import sys

import pytest

from mama import util


@pytest.fixture(autouse=True)
def _fresh_shim():
    """The shim binds a name on first use and warns once. Both are process state, so undo them."""
    yield
    for name in list(util._MOVED):
        util.__dict__.pop(name, None)
    util._warned.clear()


# The exact lines found in KrattGCS and krattcam, four of them in repos this project cannot patch.
@pytest.mark.parametrize('name', ['console', 'get_time_str', 'warning', 'path_join', 'normalized_path'])
def test_the_imports_real_mamafiles_use_still_resolve(name):
    assert getattr(util, name) is not None


def test_a_bare_import_of_the_module_does_not_raise():
    """udp_quality's mamafile does `import mama.util` and never reads an attribute."""
    importlib.import_module('mama.util')


def test_every_moved_name_resolves_to_its_new_home():
    for name, module in util._MOVED.items():
        home = importlib.import_module(f'mama.utils.{module}')
        assert getattr(util, name) is getattr(home, name), f'{name} != mama.utils.{module}.{name}'


def test_an_unknown_name_still_raises_attribute_error():
    with pytest.raises(AttributeError, match='no attribute'):
        util.this_name_never_existed


def test_the_warning_names_the_new_home_once_per_name(capsys):
    util.path_join
    first = capsys.readouterr().out
    assert 'mama.util.path_join' in first and 'mama.utils.paths' in first

    util.path_join  # bound in globals() by now, so __getattr__ cannot run again
    assert 'path_join' not in capsys.readouterr().out


def test_writable_state_warns_that_a_write_does_not_reach_the_module(capsys):
    """Assigning util.memoize_git_fingerprints lands here, not on git_status. Silence would be a trap."""
    util.memoize_git_fingerprints
    assert 'does NOT reach' in capsys.readouterr().out


def test_dir_lists_the_moved_names():
    listed = dir(util)
    assert 'path_join' in listed and 'download_file' in listed
    assert set(util.__all__) <= set(listed)
    assert not any(n.startswith('_') for n in util.__all__)


def test_mama_itself_never_touches_the_shim():
    """mama imports from mama/utils/ directly. A single internal import of mama.util would print a
    deprecation line on every build for every user."""
    code = 'import sys, mama; print("mama.util" in sys.modules)'
    out = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip().splitlines()[-1] == 'False'
    assert 'moved to' not in out.stdout


def test_no_mama_source_and_no_test_imports_the_shim():
    """The shim exists for mamafiles in repos mama does not control. A mama module or a test that
    imports it prints a deprecation line the reader can do nothing about, and a function-local import
    hides from a line-start grep, which is exactly how three of them survived the split."""
    import os, re
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    pattern = re.compile(r'from (?:\.{1,2}|mama\.)util import|from mama import util\b|import mama\.util\b')
    hits = []
    for folder in ('mama', 'tests'):
        for current, _, files in os.walk(os.path.join(root, folder)):
            if '__pycache__' in current: continue
            for name in files:
                if not name.endswith('.py') or name in ('util.py', 'test_shim_compat.py'): continue
                path = os.path.join(current, name)
                with open(path, encoding='utf-8') as f:
                    hits += [f'{os.path.relpath(path, root)}:{n}' for n, line in enumerate(f, 1)
                             if pattern.search(line)]
    assert not hits, f'these import the deprecated shim instead of mama/utils/: {hits}'


def test_the_shim_imports_nothing_costly():
    """The shim resolves lazily, so importing it must not pull the heavy modules back in."""
    code = ('import sys, mama.util, json; '
            "print(json.dumps([m for m in ('ssl','zipfile','psutil','ftplib') if m in sys.modules]))")
    out = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip().splitlines()[-1] == '[]'
