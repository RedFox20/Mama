"""Pins the skim stage: it names the children of a dep, and it touches nothing else."""
import os
import shutil
import pytest

from testutils import make_mock_dep, make_mock_local_dep

HOOK = 'import mama\nclass pkg(mama.BuildTarget):\n'


def _dep_with_mamafile(tmp_path, body, child='child'):
    """A local dep whose mamafile carries `body`, with a sibling dir ready for add_local."""
    src = tmp_path / 'pkg'; src.mkdir()
    (src / 'mamafile.py').write_text(HOOK + body)
    (tmp_path / child).mkdir(exist_ok=True)
    dep = make_mock_local_dep(tmp_path, src_dir=src, name='pkg')
    shutil.rmtree(dep.build_dir)   # make_mock_local_dep creates it, and the skim must not put it back
    return dep


def test_a_skim_names_the_children(tmp_path):
    dep = _dep_with_mamafile(tmp_path, f"    def dependencies(self): self.add_local('child', r'{tmp_path}/child')\n")
    dep.skim()
    assert [c.name for c in dep.get_children()] == ['child']


def test_a_skim_creates_no_build_dir(tmp_path):
    dep = _dep_with_mamafile(tmp_path, '    def settings(self): self.version = 1\n')
    dep.skim()
    assert not os.path.exists(dep.build_dir)   # a dep the walk only passed through owns no build dir


def test_a_skim_does_not_count_as_a_load(tmp_path):
    dep = _dep_with_mamafile(tmp_path, '    def settings(self): pass\n')
    dep.skim()
    assert dep.did_skim and not dep.already_loaded and not dep.skimming


def test_a_load_after_a_skim_runs_neither_hook_again(tmp_path):
    # add_child refuses a child it already holds, so a repeated dependencies() would raise
    dep = _dep_with_mamafile(tmp_path, f"    def dependencies(self): self.add_local('child', r'{tmp_path}/child')\n")
    dep.skim()
    dep._load()
    assert [c.name for c in dep.get_children()] == ['child']


def test_a_second_skim_is_a_no_op(tmp_path):
    dep = _dep_with_mamafile(tmp_path, f"    def dependencies(self): self.add_local('child', r'{tmp_path}/child')\n")
    dep.skim()
    dep.skim()   # a diamond dep is reached through two parents
    assert len(dep.get_children()) == 1


@pytest.mark.parametrize('call', ['self.build_dir()', 'self.source_dir()'])
def test_a_hook_that_reads_a_dep_path_raises(tmp_path, call):
    dep = _dep_with_mamafile(tmp_path, f'    def settings(self): {call}\n')
    with pytest.raises(RuntimeError, match='explores the graph'):
        dep.skim()


def test_a_dep_with_no_source_dir_raises_instead_of_returning_none(tmp_path):
    # an artifactory package has no source dir, and a silent None writes the copy outside the dep
    dep = make_mock_dep(tmp_path)
    dep.create_build_target()
    dep.src_dir = None
    with pytest.raises(RuntimeError, match='no path'):
        dep.target.source_dir()
