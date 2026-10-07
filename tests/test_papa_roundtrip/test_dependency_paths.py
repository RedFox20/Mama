import os

import pytest
from testutils import make_mock_local_dep, papa_deploy_target

from mama.dependency_lock import DependencyLock, LockEntry, LockSelector
from mama.papa_deploy import PapaFileInfo
from mama.types.git import Git
from mama.types.local_source import LocalSource
from mama.utils.paths import forward_slashes, normalized_path


def _git(mamafile):
    return Git('child', 'https://example.com/child.git', 'develop', '', mamafile, True,
               ['feature'], version_suffix='recipe2')


def _graph(tmp_path, parent_recipe=False):
    project = tmp_path / 'project'
    helper = project / 'mamadeps' / 'child.py'
    helper.parent.mkdir(parents=True)
    helper.write_text('', encoding='utf-8')
    (project / 'modules' / 'module').mkdir(parents=True)
    recipe = None
    if parent_recipe:
        recipe = 'recipes/module.py'
        (project / recipe).parent.mkdir()
        (project / recipe).write_text('import mama\nclass module(mama.BuildTarget): pass\n', encoding='utf-8')
    root = make_mock_local_dep(tmp_path / 'cache', project, name='root')
    entry = LockEntry('child', 'example.com/child', LockSelector('branch', 'develop'), 'a' * 40)
    root.config.dependency_lock = DependencyLock(str(project / 'mama.lock'), ('linux',), {'child': entry})
    module = root.add_child(LocalSource('module', 'modules/module', recipe, False, []))
    return root, module, helper


def _deploy(dep, child, dest):
    papa_deploy_target(dep.target, str(dest), children=[child])
    return PapaFileInfo(str(dest / 'papa.txt'))


@pytest.mark.parametrize('root_first', [True, False], ids=['root-first', 'module-first'])
@pytest.mark.parametrize('parent_recipe', [False, True], ids=['source-dir', 'mamafile-dir'])
def test_shared_git_recipe_round_trip_matches_the_lock(tmp_path, root_first, parent_recipe):
    root, module, helper = _graph(tmp_path / 'producer', parent_recipe)
    module_path = forward_slashes(os.path.relpath(helper, module.path_relative_to_us('.')))
    declarations = [(root, 'mamadeps/child.py'), (module, module_path)]
    if not root_first: declarations.reverse()
    for parent, path in declarations:
        child = parent.add_child(_git(path))
    assert child is root.children[-1] is module.children[0]
    original = child.dep_source.declaration()

    for exporter in (module, root):
        papa = _deploy(exporter, child, tmp_path / f'{exporter.name}-package')
        exported = papa.dependencies[0]
        expected = forward_slashes(os.path.relpath(helper, exporter.path_relative_to_us('.')))
        assert exported.mamafile == expected
        assert child.dep_source.declaration() == original
        assert exported.url == child.dep_source.url and exported.branch == 'develop'
        assert exported.args == ['feature'] and exported.version_suffix == 'recipe2'

        consumer, consumer_module, consumer_helper = _graph(tmp_path / f'{exporter.name}-consumer', parent_recipe)
        parent = consumer_module if exporter is module else consumer
        if exporter is module: consumer.add_child(_git('mamadeps/child.py'))
        parent.add_children(papa.dependencies)
        imported = parent.children[-1]
        assert imported.mamafile == normalized_path(str(consumer_helper))
        assert imported.dep_source.locked_commit == 'a' * 40


@pytest.mark.parametrize('absolute', [False, True], ids=['default', 'absolute'])
def test_default_and_absolute_git_recipes_keep_their_declaration(tmp_path, absolute):
    root, module, helper = _graph(tmp_path)
    mamafile = normalized_path(str(helper)) if absolute else None
    child = root.add_child(_git(mamafile))
    papa = _deploy(module, child, tmp_path / 'package')
    assert papa.dependencies[0].mamafile == (mamafile or '')
    assert child.dep_source.mamafile == mamafile
