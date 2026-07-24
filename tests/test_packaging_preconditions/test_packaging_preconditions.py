"""Pins that package() only runs when there is something to package, and that a re-loaded artifactory
package does not re-add children it already added."""
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from testutils import make_mock_dep

from mama.build_target import BuildTarget


def _target(tmp_path, built=False, **dep_over):
    """A BuildTarget whose package() records whether it ran; `built` fakes real build work."""
    dep = make_mock_dep(tmp_path)
    dep.nothing_to_build = False
    dep.from_artifactory = False
    dep.should_rebuild = built
    for k, v in dep_over.items(): setattr(dep, k, v)
    target = BuildTarget(name=dep.name, config=dep.config, dep=dep, args=[])
    dep.target = target
    target.package = Mock()
    return target


def test_packaging_is_skipped_when_nothing_was_built_and_nothing_is_on_disk(tmp_path):
    # `mama wipe all` deletes every build product, then walks the task chain without building; the
    # user's package() then asserted on libs that no longer exist
    target = _target(tmp_path)
    target._run_packaging()
    target.package.assert_not_called()


def test_packaging_runs_after_a_real_build(tmp_path):
    target = _target(tmp_path, built=True)
    target._run_packaging()
    target.package.assert_called_once()


def test_packaging_runs_for_an_already_built_tree(tmp_path):
    # `mama upload` re-packages without rebuilding; the artifacts are there, so it must still run
    target = _target(tmp_path)
    (tmp_path / 'packages/libfoo/linux/CMakeCache.txt').write_text('')
    target._run_packaging()
    target.package.assert_called_once()


def test_packaging_runs_for_a_header_only_target(tmp_path):
    # nothing_to_build targets never build but still export includes
    target = _target(tmp_path, nothing_to_build=True)
    target._run_packaging()
    target.package.assert_called_once()


def test_an_artifactory_package_is_not_repackaged(tmp_path):
    # pre-existing behaviour the new guard must leave alone: a fetched package already has its
    # papa.txt exports, so package() is skipped unless a local rebuild was asked for
    target = _target(tmp_path, from_artifactory=True)
    target._run_packaging()
    target.package.assert_not_called()


def test_skip_is_announced_when_the_user_asked_to_deploy(tmp_path, capsys):
    target = _target(tmp_path)
    target.config.upload = True
    target.config.print = True
    target._run_packaging()
    assert 'PACKAGE skipped' in capsys.readouterr().out  # else `mama upload` fails with no explanation


def test_build_phase_still_packages_what_it_just_built(tmp_path):
    # the guard must not break the normal path: build work happened -> package regardless of disk state
    target = _target(tmp_path, built=True)
    with patch.object(BuildTarget, '_has_custom_build', return_value=False), \
         patch.object(BuildTarget, '_cmake_build_step'), patch('mama.build_target.package'):
        target.build_phase()
    target.package.assert_called_once()


def _declared(dep, *names):
    """papa.txt-style dep_sources, pre-registered so add_child reuses them instead of cloning."""
    for name in names:
        dep.config.loaded_dependencies[name] = SimpleNamespace(name=name, update_existing_dependency=lambda s: None)
    return [SimpleNamespace(name=name) for name in names]


def test_reloading_an_artifactory_package_does_not_re_add_its_children(tmp_path):
    # first-time `mama clean all`: the shim probe adds papa.txt's deps, then the post-clean re-extract
    # reports the same list - add_child's duplicate raise used to kill the whole run
    dep = make_mock_dep(tmp_path)
    sources = _declared(dep, 'ReCpp', 'zlib')
    dep.add_children(sources)  # shim probe
    dep.add_children(sources)  # post-clean re-extract
    assert [c.name for c in dep.children] == ['ReCpp', 'zlib']


def test_a_genuine_duplicate_declaration_still_raises(tmp_path):
    # add_children must not mask a mamafile that declares the same dep twice
    dep = make_mock_dep(tmp_path)
    source = _declared(dep, 'ReCpp')[0]
    dep.add_child(source)
    with pytest.raises(RuntimeError, match='already been added'):
        dep.add_child(source)
