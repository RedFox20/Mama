"""Pins user_cache_dir: the override wins, the result is forward-slashed and it leaves the workspace."""
import os
import pytest

from mama.utils import paths as util


@pytest.fixture(autouse=True)
def _fresh_cache():
    """Production reads MAMA_CACHE_DIR once, so both helpers memoize. A test that changes the env has to
    drop the memo, or it reads what the test before it set."""
    util.user_cache_dir.cache_clear(); util._cache_base.cache_clear()
    yield
    util.user_cache_dir.cache_clear(); util._cache_base.cache_clear()


def test_the_env_override_wins_and_takes_the_parts(monkeypatch):
    monkeypatch.setenv('MAMA_CACHE_DIR', 'D:\\ci\\cache')
    assert util.user_cache_dir('compiler_seed') == 'D:/ci/cache/compiler_seed'
    assert util.user_cache_dir() == 'D:/ci/cache'


@pytest.mark.parametrize('windows, macos, env, expect', [
    (True,  False, ('LOCALAPPDATA', 'C:\\Users\\x\\AppData\\Local'), 'C:/Users/x/AppData/Local/mama/seed'),
    (False, False, ('XDG_CACHE_HOME', '/home/x/.cache'),             '/home/x/.cache/mama/seed'),
])
def test_each_platform_uses_its_own_cache_convention(monkeypatch, windows, macos, env, expect):
    monkeypatch.delenv('MAMA_CACHE_DIR', raising=False)
    monkeypatch.setattr(util.System, 'windows', windows)
    monkeypatch.setattr(util.System, 'macos', macos)
    monkeypatch.setenv(*env)
    assert util.user_cache_dir('seed') == expect


def test_no_home_falls_back_to_the_temp_dir(monkeypatch, tmp_path):
    # a container with no HOME must still get a writable seed root, not a literal '~/.cache'
    monkeypatch.delenv('MAMA_CACHE_DIR', raising=False)
    monkeypatch.setattr(util.System, 'windows', False)
    monkeypatch.setattr(util.System, 'macos', False)
    monkeypatch.delenv('XDG_CACHE_HOME', raising=False)
    monkeypatch.setattr(util.os.path, 'expanduser', lambda p: p)  # what expanduser does with no home
    monkeypatch.setattr(util.tempfile, 'gettempdir', lambda: str(tmp_path))
    assert util.user_cache_dir('seed') == f'{util.forward_slashes(str(tmp_path))}/mama/seed'


def test_the_seed_root_is_not_inside_the_workspace(monkeypatch):
    # the point of the change: a new checkout must not pay the 4-second compiler probe again
    monkeypatch.setenv('MAMA_CACHE_DIR', '/var/cache/mama')
    assert not util.user_cache_dir('compiler_seed').startswith(os.getcwd().replace('\\', '/'))
