"""Pins thread-safety of parallel dependency loading: concurrent add_child dedups a shared (diamond)
dep to one instance; concurrent load() runs the dep's body exactly once."""
import threading, time
from testutils import make_mock_config
from mama.build_dependency import BuildDependency
from mama.types.local_source import LocalSource
from mama.types.git import Git


def _root(config, tmp_path, name):
    sub = tmp_path / name; sub.mkdir()
    return BuildDependency(None, config, 'packages', LocalSource(name, str(sub), None, False, []))


def test_concurrent_add_child_dedups_diamond_dep(tmp_path):
    config = make_mock_config(tmp_path)
    p1 = _root(config, tmp_path, 'p1'); p2 = _root(config, tmp_path, 'p2')
    barrier = threading.Barrier(2); got = {}
    def add(p, key):
        barrier.wait()
        got[key] = p.add_child(Git('shared', 'https://x/shared.git', 'main', '', None, True, []))
    ts = [threading.Thread(target=add, args=(p, k)) for p, k in ((p1, 1), (p2, 2))]
    for t in ts: t.start()
    for t in ts: t.join(5)
    assert len(config.loaded_dependencies) == 1   # one shared instance; no race-created duplicate
    assert got[1] is got[2] and p1.children[0] is p2.children[0]


def test_concurrent_load_runs_body_once(tmp_path, monkeypatch):
    dep = _root(make_mock_config(tmp_path), tmp_path, 'x')
    calls = []
    def fake_load():
        calls.append(1); time.sleep(0.05)
        dep.already_loaded = True; dep.should_rebuild = False
        return False
    monkeypatch.setattr(dep, '_load', fake_load)
    barrier = threading.Barrier(3)
    def go(): barrier.wait(); dep.load()
    ts = [threading.Thread(target=go) for _ in range(3)]
    for t in ts: t.start()
    for t in ts: t.join(5)
    assert len(calls) == 1   # 3 concurrent load() calls -> _load body ran exactly once
