"""Pins the two rules that keep a rebuild cheap across a deep -> middle -> root chain.

A rebuilt dependency propagates a BUILD to everything above it, because a relink needs the new lib.
It propagates a CONFIGURE only when its interface changed, because cmake learns nothing new otherwise.
"""
import pytest

from testutils import (make_configured_target, make_mock_dep, run_config_capturing,
                       write_dep_exports as _exports)


def _chain(tmp_path):
    """deep -> middle -> root, each a real BuildDependency whose children the parent reads."""
    deps = {}
    for name in ('deep', 'middle', 'root'):
        dep = make_mock_dep(tmp_path / name, name=name, build=True)
        dep.config.no_specific_target.return_value = True
        deps[name] = dep
    deps['middle'].children = [deps['deep']]
    deps['root'].children = [deps['middle']]
    return deps


def test_a_rebuilt_dependency_makes_every_parent_build(tmp_path):
    # deep produced a new static lib, so middle must relink, and root must relink after middle
    deps = _chain(tmp_path)
    deps['deep'].should_rebuild = True

    deps['middle'].after_load()
    assert deps['middle'].should_rebuild is True

    deps['root'].after_load()   # runs after middle, which is the order both schedulers use
    assert deps['root'].should_rebuild is True


def test_a_quiet_dependency_makes_no_parent_build(tmp_path):
    deps = _chain(tmp_path)
    deps['middle'].after_load()
    deps['root'].after_load()
    assert deps['middle'].should_rebuild is False and deps['root'].should_rebuild is False


# -- the configure half: only an interface change reaches cmake ----------------

def _consumer(tmp_path):
    """A target that already configured once, with the exports of its dependencies recorded."""
    t, dep = make_configured_target(tmp_path, update=True, run_cmake_configure=False)
    _exports(t, 'set(deep_INCLUDES /deep/include)\nset(deep_LIBS /deep/lib/deep.a)\n')
    assert _configure(t, dep) is True    # the first configure of the build dir
    assert _configure(t, dep) is False   # nothing changed since, so the gate is armed
    return t, dep


def _configure(t, dep) -> bool:
    return bool(run_config_capturing(t, dep, leave_build_dir=True))


def test_a_rebuilt_dependency_alone_needs_no_configure(tmp_path):
    # THE case: deep rebuilt its static lib at the same path, so the consumer relinks and cmake
    # learns nothing. A warm configure of a real project costs about 50 seconds.
    t, dep = _consumer(tmp_path)
    assert _configure(t, dep) is False


@pytest.mark.parametrize('interface', ['set(deep_INCLUDES /deep/include /deep/include2)\nset(deep_LIBS /deep/lib/deep.a)\n',
                                       'set(deep_INCLUDES /deep/include)\nset(deep_LIBS /deep/lib/deep.a /deep/lib/extra.a)\n'])
def test_a_changed_interface_does_need_a_configure(tmp_path, interface):
    # a new include dir or a new export lib changes what cmake must know
    t, dep = _consumer(tmp_path)
    _exports(t, interface)
    assert _configure(t, dep) is True
