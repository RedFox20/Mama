"""Pins where the workspace lands: the project dir, unless the root mamafile asks for the global one."""
import os
import pytest

from mama.build_config import BuildConfig
from mama.build_dependency import BuildDependency
from mama.types.local_source import LocalSource

ROOT_MAMAFILE = 'import mama\nclass proj(mama.BuildTarget):\n    {}\n    def dependencies(self): pass\n'


def _root(tmp_path, mamafile_body=None):
    (tmp_path / 'CMakeLists.txt').write_text('project(proj)\n')
    if mamafile_body is not None:
        (tmp_path / 'mamafile.py').write_text(ROOT_MAMAFILE.format(mamafile_body))
    cfg = BuildConfig(['list'])
    cfg.root_source_dir = str(tmp_path)
    dep = BuildDependency(None, cfg, None, LocalSource('proj', str(tmp_path), None, False, []))
    dep.load()
    return cfg, dep


def test_a_root_with_no_mamafile_keeps_its_packages_in_the_project(tmp_path):
    # only the mamafile parse used to assign this, so a CMakeLists-only project wrote into the home dir
    cfg, dep = _root(tmp_path)
    assert cfg.workspaces_root == str(tmp_path)
    assert dep.dep_dir.startswith(str(tmp_path))
    assert not dep.dep_dir.startswith(os.path.expanduser('~') + '/packages')


@pytest.mark.parametrize('body', ['local_workspace = "packages"', 'workspace = "packages"', 'pass'])
def test_a_local_workspace_stays_in_the_project(tmp_path, body):
    cfg, dep = _root(tmp_path, body)
    assert cfg.workspaces_root == str(tmp_path)


def test_a_global_workspace_leaves_the_project(tmp_path):
    cfg, dep = _root(tmp_path, 'global_workspace = "packages"')
    assert cfg.workspaces_root != str(tmp_path)   # the user home dir, so one tree serves every checkout
