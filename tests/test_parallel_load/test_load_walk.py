"""Pins which deps the load walk enters, that one thread owns each load, and one live line per dep."""
import io, threading, time
from testutils import FakeWalkDep, make_walk_config, summary_lines
from mama.dependency_chain import load_dependency_chain
from mama.utils.build_display import BuildDisplay
from mama.utils.system import console


def _walk_with_display(root, **kw):
    """Walk `root` under a non-tty display and return the summary line it committed per dep. A non-tty
    run also opens each phase with a `>` line, which these tests skip. reveal_delay=0 shows every dep,
    because a fake load takes no time at all."""
    out = io.StringIO()
    display = BuildDisplay(out, isatty=False, term_size=lambda: (200, 24), clock=lambda: 0.0,
                           color=False, reveal_delay=0, **kw)
    load_dependency_chain(root, display)
    display.close()
    return summary_lines(out.getvalue())


def test_the_walk_enters_the_dep_it_starts_from_even_when_loaded():
    # mamabuild loads the root before the walk, and a reload revives deps below an already loaded scope
    log = []; cfg = make_walk_config()
    child = FakeWalkDep('child', cfg, log)
    load_dependency_chain(FakeWalkDep('root', cfg, log, [child], loaded=True))
    assert log == ['root', 'child']  # the entry replays its load, and its children still load


def test_the_walk_stops_at_a_dep_another_parent_already_walked():
    log = []; cfg = make_walk_config()
    shared = FakeWalkDep('shared', cfg, log, [FakeWalkDep('below', cfg, log)], loaded=True)
    load_dependency_chain(FakeWalkDep('root', cfg, log, [shared]))
    assert log == ['root']  # the first parent to reach a shared dep walks it and its subtree, once


def test_a_revived_dep_below_a_loaded_scope_loads():
    # reload_deferred_deps clears already_loaded on the revived dep, then walks the scope again
    log = []; cfg = make_walk_config()
    revived = FakeWalkDep('revived', cfg, log)
    scope = FakeWalkDep('scope', cfg, log, [revived], loaded=True)
    load_dependency_chain(scope)
    assert log == ['scope', 'revived']


def test_each_dep_commits_one_load_line():
    log = []; cfg = make_walk_config()
    child = FakeWalkDep('child', cfg, log)
    lines = _walk_with_display(FakeWalkDep('root', cfg, log, [child]))
    assert len(lines) == 2 and 'root' in lines[0] and 'child' in lines[1]


def test_a_dep_reports_what_its_load_did_not_the_opening_guess():
    log = []; cfg = make_walk_config()
    dep = FakeWalkDep('dep', cfg, log)
    dep.load_action = 'artifactory'; dep.artifactory_archive = 'dep-linux-x64-release-abc1234'
    line = _walk_with_display(dep)[0]
    assert 'artifactory' in line and 'clone' not in line  # relabeled from the optimistic opening label
    assert 'dep-linux-x64-release-abc1234' in line        # and it names the package it unpacked


def _diamond(cfg, log, shared):
    """A root with two parents of one shared dep. The barrier holds both parents until they load
    together, so they reach the shared dep at the same time and one of them must wait."""
    gate = threading.Barrier(2, timeout=5)
    parents = [FakeWalkDep(name, cfg, log, [shared], on_load=gate.wait) for name in ('A', 'B')]
    return FakeWalkDep('root', cfg, log, parents), parents


def test_only_one_parent_loads_a_shared_dep_and_walks_its_subtree():
    cfg = make_walk_config(serial_load=False)
    log = []
    below = FakeWalkDep('below', cfg, log)
    shared = FakeWalkDep('shared', cfg, log, [below], on_load=lambda: time.sleep(0.05))
    root, _ = _diamond(cfg, log, shared)
    lines = _walk_with_display(root)
    assert log.count('shared') == 1 and log.count('below') == 1  # the waiting parent walks nothing
    assert sum('shared' in line for line in lines) == 1          # and it draws no second line


def test_a_waiting_parent_reads_the_finished_rebuild_flag():
    # a parent that returned early would read a half-loaded dep, and its after_load would miss the change
    cfg = make_walk_config(serial_load=False)
    log = []
    def slow_load(): time.sleep(0.05); shared.should_rebuild = True
    shared = FakeWalkDep('shared', cfg, log, on_load=slow_load)
    root, parents = _diamond(cfg, log, shared)
    seen = {}
    for p in parents: p.after_load = lambda p=p: seen.__setitem__(p.name, shared.should_rebuild)
    load_dependency_chain(root)
    assert seen == {'A': True, 'B': True}


def test_a_failed_load_releases_the_waiting_parent():
    cfg = make_walk_config(serial_load=False)
    log = []
    def boom(): time.sleep(0.05); raise RuntimeError('clone died')
    root, _ = _diamond(cfg, log, FakeWalkDep('shared', cfg, log, on_load=boom))
    def walk_until_it_raises():
        try: load_dependency_chain(root)
        except RuntimeError: pass
    walk = threading.Thread(target=walk_until_it_raises, daemon=True)
    walk.start(); walk.join(10)
    assert not walk.is_alive()  # a waiter left blocked would hang the whole load


def test_the_output_of_a_load_feeds_its_own_line_and_not_the_terminal(capsys):
    log = []; cfg = make_walk_config()
    noisy = FakeWalkDep('noisy', cfg, log, on_load=lambda: console('CLONE because src is missing'))
    text = '\n'.join(_walk_with_display(FakeWalkDep('root', cfg, log, [noisy]), verbose=True))
    assert 'CLONE because src is missing' in text  # captured into the task of the dep
    assert 'CLONE' not in capsys.readouterr().out  # and never printed straight to the terminal
