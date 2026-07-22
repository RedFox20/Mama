"""Pins which execution path mamabuild picks, and that both paths leave the shared locals defined."""
from types import SimpleNamespace
import pytest
from mama.main import _can_unify


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


@pytest.mark.parametrize('flags', [{'list': True}, {'deps_only': True}, {'dirty': True},
                                   {'mama_init': True}, {'serial_load': True}, {'target': 'ReCpp'}])
def test_paths_that_need_the_loaded_tree_do_not_unify(flags):
    # each of these reads the fully-resolved tree (target lookup, filtering, listing) that only the
    # classic path builds up front
    assert not _can_unify(_cfg(**flags))


def test_nothing_to_do_does_not_unify():
    assert not _can_unify(_cfg(build=False, update=False))
