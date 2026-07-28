"""Pins execute_unified: the graph grows as fake LOADs discover children, and a parent only
configures after its children have built (leaf nodes build while deeper deps still load)."""
import threading
from testutils import FakeUnifiedDep, make_unified_config
from mama import dependency_chain as dc
from mama.utils import system


def test_unified_grows_graph_and_orders_parent_after_children(no_cmake_writes):
    cfg = make_unified_config()
    ev, lock = [], threading.Lock()
    # root -> {A (leaf), B -> {C (leaf)}}
    root = FakeUnifiedDep('root', cfg, ev, lock, child_specs=[('A', ()), ('B', [('C', ())])])
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


def test_unified_dedups_a_diamond_dependency(no_cmake_writes):
    cfg = make_unified_config()
    ev, lock = [], threading.Lock()
    d = FakeUnifiedDep('D', cfg, ev, lock)                       # one shared instance...
    a = FakeUnifiedDep('A', cfg, ev, lock, shared_children=[d])   # ...reached via both A...
    b = FakeUnifiedDep('B', cfg, ev, lock, shared_children=[d])   # ...and B (diamond)
    dc.execute_unified(FakeUnifiedDep('root', cfg, ev, lock, shared_children=[a, b]))
    names = lambda tag: [n for t, n in ev if t == tag]
    assert names('load').count('D') == 1   # grow() dedups the shared child: cloned once, not per-parent
    assert names('bld').count('D') == 1     # and built once


def test_unified_loads_the_root_before_the_display_exists(no_cmake_writes, monkeypatch):
    """The root's settings() picks the toolchain everything else needs, so it runs first - and its
    output must reach the terminal instead of a captured task line nobody scrolls back to."""
    cfg = make_unified_config()
    ev, lock = [], threading.Lock()
    make_display = dc._make_display
    monkeypatch.setattr(dc, '_make_display', lambda c: (ev.append(('display', '-')), make_display(c))[1])
    root = FakeUnifiedDep('root', cfg, ev, lock)
    sinks, load = [], root.load
    root.load = lambda: (sinks.append(system.capture_context()[0]), load())[1]
    dc.execute_unified(root)

    assert ev.index(('load', 'root')) < ev.index(('display', '-'))
    assert sinks[0] is None  # no capture sink: settings() prints to the terminal (the L job replays it captured)


def test_unified_propagates_a_changed_child_up_to_its_parent(no_cmake_writes, monkeypatch):
    """after_load() is what turns 'child rebuilt' into 'parent rebuilds too'. The unified path has to
    run it per dep at configure time - by then every child has loaded AND built."""
    ev, lock = [], threading.Lock()
    root = FakeUnifiedDep('root', make_unified_config(), ev, lock, child_specs=[('A', ())])
    seen = []
    root.after_load = lambda: seen.append([c.should_rebuild for c in root.get_children()])
    old_load = FakeUnifiedDep.load
    monkeypatch.setattr(FakeUnifiedDep, 'load', lambda d: (old_load(d), setattr(d, 'should_rebuild', d.name == 'A'))[0])
    dc.execute_unified(root)
    assert seen == [[True]]  # A's load result is visible to the root's after_load
