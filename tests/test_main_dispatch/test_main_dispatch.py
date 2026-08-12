"""Pins which execution path mamabuild picks, and that both paths leave the shared locals defined."""
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock, patch
import pytest
from testutils import make_project_dir, stub_loaders
from mama.main import _can_unify, print_package_exports, mamabuild


def _cfg(**over):
    c = SimpleNamespace(serial_load=False, build=True, update=False, list=False, deps_only=False,
                        dirty=False, mama_init=False, target=None)
    c.no_specific_target = lambda: c.target in (None, 'all')
    for k, v in over.items(): setattr(c, k, v)
    return c


def test_a_plain_full_build_unifies():
    assert _can_unify(_cfg())
    assert _can_unify(_cfg(target='all'))     # `all` is not a specific target
    assert _can_unify(_cfg(build=False, update=True))


def test_deps_only_unifies_with_and_without_a_target():
    assert _can_unify(_cfg(deps_only=True, target='all'))
    assert _can_unify(_cfg(deps_only=True, target='ReCpp'))  # the scheduler scopes it to ReCpp's deps


@pytest.mark.parametrize('flags', [{'list': True}, {'dirty': True},
                                   {'mama_init': True}, {'serial_load': True}, {'target': 'ReCpp'}])
def test_paths_that_need_the_loaded_tree_do_not_unify(flags):
    # each of these reads the fully-resolved tree (target lookup, filtering, listing) that only the
    # classic path builds up front
    assert not _can_unify(_cfg(**flags))


def test_nothing_to_do_does_not_unify():
    assert not _can_unify(_cfg(build=False, update=False))


def _listed(archive: str) -> str:
    """The one line print_package_exports writes for a dep that loaded from artifactory."""
    dep = SimpleNamespace(from_artifactory=True, artifactory_archive=archive,
                          target=Mock(name='target', print_exports=Mock()))
    dep.target.name = 'googletest'
    with patch('mama.main.console') as out: print_package_exports(dep)
    return out.call_args[0][0]


def test_the_listing_names_the_archive_the_exports_came_from():
    assert 'googletest-ubuntu-24-x64-release-ae51a95' in _listed('googletest-ubuntu-24-x64-release-ae51a95')


def test_the_listing_reads_clean_when_no_archive_is_known():
    assert _listed('').rstrip().endswith('fetched from artifactory')


def test_mamabuild_loads_the_root_then_opens_one_log_before_it_dispatches(tmp_path):
    # the root load names the workspace the log lives in, so the order is load, open, dispatch
    order = []
    def open_log(*args): order.append('log'); return 'LOG'
    with stub_loaders(lambda r: None), patch('mama.main.print_build_banner'), \
         patch('mama.main.load_root', side_effect=lambda r: order.append('load')) as load, \
         patch('mama.main.open_run_log', side_effect=open_log) as opened, patch('mama.main.set_run_log') as wired, \
         patch('mama.main.execute_unified', side_effect=lambda *a, **k: order.append('dispatch')):
        mamabuild(['build'], source_dir=make_project_dir(tmp_path))
    assert order == ['load', 'log', 'dispatch']
    assert load.call_count == 1 and opened.call_count == 1  # one run, one root load, one log
    wired.assert_called_once_with('LOG')  # console() logs the lines that no display owns


def test_the_classic_path_closes_its_live_region_before_the_package_listing(tmp_path):
    order = []
    @contextmanager
    def fake_region(config):
        order.append('open'); yield 'region'; order.append('close')
    with patch('mama.main.load_display', fake_region), patch('mama.main.execute_task_chain_parallel'), \
         patch('mama.main.load_dependency_chain', side_effect=lambda r, d=None: order.append(('load', d))), \
         patch('mama.main.print_package_exports', side_effect=lambda d: order.append('listing')):
        mamabuild(['list'], source_dir=make_project_dir(tmp_path))
    assert order == ['open', ('load', 'region'), 'close', 'listing']


def test_a_stop_signal_becomes_the_interrupt_mama_already_handles():
    # the default SIGTERM action ends the process at once, and every buffered phase dies unread
    import signal
    from mama.main import install_stop_signals
    previous = signal.getsignal(signal.SIGTERM)
    try:
        install_stop_signals()
        with pytest.raises(KeyboardInterrupt):
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
    finally:
        signal.signal(signal.SIGTERM, previous)
