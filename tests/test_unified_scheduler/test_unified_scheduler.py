"""Pins execute_unified: the graph grows as fake LOADs discover children, and a parent only
configures after its children have built (leaf nodes build while deeper deps still load)."""
import threading
from types import SimpleNamespace
from unittest.mock import Mock
from testutils import FakeBuildTarget
from mama import dependency_chain as dc


class _Target(FakeBuildTarget):
    def __init__(self, dep, ev, lock):
        self.dep = dep; self._ev = ev; self._lock = lock; self._out_sink = None
    def _rec(self, tag):
        with self._lock: self._ev.append((tag, self.dep.name))
    def configure_phase(self, out=None): self._rec('cfg')
    def build_phase(self, out=None): self._rec('bld')
    def _execute_deploy_tasks(self): pass
    def _execute_run_tasks(self): pass


class _Dep:
    def __init__(self, name, config, ev, lock, child_specs=()):
        self.name = name; self.config = config; self._ev = ev; self._lock = lock
        self._child_specs = child_specs; self._children = []; self.already_executed = False
        self.target = _Target(self, ev, lock)
    def load(self):
        with self._lock: self._ev.append(('load', self.name))
        self._children = [_Dep(n, self.config, self._ev, self._lock, cs)   # discovered only now
                          for n, cs in self._child_specs]
    def get_children(self): return self._children
    def is_root_or_config_target(self): return False


def test_unified_grows_graph_and_orders_parent_after_children(monkeypatch):
    monkeypatch.setattr(dc, '_save_mama_cmake_and_dependencies_cmake', lambda d: None)
    monkeypatch.setattr(dc, '_save_vscode_compile_commands', lambda d: None)
    cfg = SimpleNamespace(jobs=2, parallel_max=8, verbose=False, test=False, update_stats=Mock())
    ev, lock = [], threading.Lock()
    # root -> {A (leaf), B -> {C (leaf)}}
    root = _Dep('root', cfg, ev, lock, child_specs=[('A', ()), ('B', [('C', ())])])
    dc.execute_unified(root)

    names = lambda tag: [n for t, n in ev if t == tag]
    assert set(names('load')) == {'root', 'A', 'B', 'C'}     # whole graph discovered dynamically
    assert set(names('bld')) == {'root', 'A', 'B', 'C'}      # everything configured+built
    idx = lambda pair: ev.index(pair)
    assert idx(('load', 'A')) > idx(('load', 'root'))        # children discovered after parent loads
    assert idx(('load', 'C')) > idx(('load', 'B'))
    assert idx(('cfg', 'B')) > idx(('bld', 'C'))             # parent configures only after child builds
    assert idx(('cfg', 'root')) > idx(('bld', 'A')) and idx(('cfg', 'root')) > idx(('bld', 'B'))
    assert idx(('bld', 'root')) > idx(('cfg', 'root'))
    assert root.already_executed
