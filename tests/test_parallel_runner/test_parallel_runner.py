"""Pins execute_task_chain_parallel: child builds before parent configures, serial deploy/run
post-pass, and fail-fast that stops dependents and exits."""
import threading
from types import SimpleNamespace
import pytest
from testutils import FakeBuildTarget
from mama import dependency_chain as dc


class _T(FakeBuildTarget):
    def __init__(self, dep, ev, lock, fail=False):
        self.dep = dep; self.ev = ev; self.lock = lock; self.fail = fail
    def _rec(self, name):
        with self.lock: self.ev.append(name)
    def configure_phase(self, out=None): self._rec(('configure', self.dep.name))
    def build_phase(self, out=None):
        self._rec(('build', self.dep.name))
        if self.fail: raise RuntimeError('boom ' + self.dep.name)
    def _execute_deploy_tasks(self): self._rec(('deploy', self.dep.name))
    def _execute_run_tasks(self): self._rec(('run', self.dep.name))


class _D:
    def __init__(self, name, config, children=()):
        self.name = name; self.config = config; self._children = list(children); self.already_executed = False
    def get_children(self): return self._children
    def is_root_or_config_target(self): return False


def _graph(monkeypatch, fail_child=False):
    monkeypatch.setattr(dc, '_save_mama_cmake_and_dependencies_cmake', lambda d: None)
    monkeypatch.setattr(dc, '_save_vscode_compile_commands', lambda d: None)
    cfg = SimpleNamespace(jobs=2, verbose=False, test=False)
    ev, lock = [], threading.Lock()
    child = _D('child', cfg); parent = _D('parent', cfg, [child])
    for d in (child, parent): d.target = _T(d, ev, lock)
    child.target.fail = fail_child
    return [child, parent], ev  # flat_deps_reverse is leaves-first


def test_parallel_runner_orders_and_runs_post_pass(monkeypatch):
    deps, ev = _graph(monkeypatch)
    dc.execute_task_chain_parallel(deps)
    assert ev.index(('build', 'child')) < ev.index(('configure', 'parent'))  # child built before parent configures
    assert ev.index(('configure', 'parent')) < ev.index(('build', 'parent'))
    assert ('deploy', 'child') in ev and ('run', 'parent') in ev            # serial post-pass ran
    assert all(d.already_executed for d in deps)


def test_parallel_runner_fails_fast_and_blocks_dependents(monkeypatch, capsys):
    deps, ev = _graph(monkeypatch, fail_child=True)
    with pytest.raises(SystemExit):
        dc.execute_task_chain_parallel(deps)
    out = capsys.readouterr().out
    assert 'BUILD FAILED' in out and 'child' in out
    assert ('build', 'parent') not in ev   # parent build depends on failed child, never released


def test_node_marker_root_leaf_trunk():
    mk = lambda root, kids: SimpleNamespace(is_root=root, get_children=lambda: kids)
    assert dc._node_marker(mk(False, [])) == '[L]'    # no deps of its own
    assert dc._node_marker(mk(False, [1])) == '[T]'   # has deps
    assert dc._node_marker(mk(True, [1])) == '[R]'    # root wins regardless of children


def test_phase_label_load_is_clone_when_fresh_else_update():
    fresh = SimpleNamespace(is_real_clone=lambda: False)
    existing = SimpleNamespace(is_real_clone=lambda: True)
    assert dc._phase_label(fresh, 'load') == 'clone' and dc._phase_label(existing, 'load') == 'update'
    assert dc._phase_label(fresh, 'configure') == 'configure'  # non-load kinds verbatim


def test_reserve_weight_is_zero_for_custom_build_else_reserved_cores():
    def mk(custom, cores):
        t = SimpleNamespace(_has_custom_build=lambda: custom, _reserved_cores=lambda: cores)
        return SimpleNamespace(target=t)
    assert dc._reserve_weight(mk(custom=False, cores=12)) == 12  # default build reserves at launch
    assert dc._reserve_weight(mk(custom=True, cores=12)) == 0    # custom build self-reserves via the barrier


def test_build_summary_counts_only_real_builds(capsys):
    d = lambda **k: SimpleNamespace(**k)
    deps = [d(should_rebuild=True, from_artifactory=False, nothing_to_build=False),    # compiled
            d(should_rebuild=True, from_artifactory=True, nothing_to_build=False),     # artifactory fetch
            d(should_rebuild=False, from_artifactory=False, nothing_to_build=False),   # up to date
            d(should_rebuild=True, from_artifactory=False, nothing_to_build=True)]     # header-only no-op
    dc._print_build_summary(deps, 72.0)
    assert 'Built 1 target(s) in 1m 12s' in capsys.readouterr().out
