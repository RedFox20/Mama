"""Pins where the compiler seed lives: the workspace by default, the user cache under `globalcache`."""
import pytest

from mama import util
from mama.buildsys.cmake import configure as cc
from testutils import make_configured_target


@pytest.fixture(autouse=True)
def _fresh_cache():
    util.user_cache_dir.cache_clear()
    yield
    util.user_cache_dir.cache_clear()  # the session cache dir must survive this file


def test_the_seed_stays_in_the_workspace_by_default(tmp_path):
    # a developer heals a broken seed with `rm -rf packages/`, which only works while it lives there
    t, _ = make_configured_target(tmp_path, global_compiler_cache=False)
    root = cc._seed_root(t)
    assert root.endswith('/.mama_compiler_seed')
    assert root.startswith(util.forward_slashes(str(tmp_path)))


def test_globalcache_moves_the_seed_to_the_user_cache(tmp_path, monkeypatch):
    # one 4-second probe then serves every checkout on the machine, which is what CI and the suite want
    monkeypatch.setenv('MAMA_CACHE_DIR', str(tmp_path / 'cache'))
    t, _ = make_configured_target(tmp_path, global_compiler_cache=True)
    assert cc._seed_root(t) == f'{util.forward_slashes(str(tmp_path))}/cache/compiler_seed'
