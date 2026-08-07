"""Pins version_suffix: it renames the package on every platform, and an empty one changes nothing."""
import pytest

from testutils import archive_name_for as _name, make_exporting_target, make_mock_dep, make_package_target

from mama.papa_deploy import PapaFileInfo, papa_deploy_to
from mama.types.artifactory_pkg import ArtifactoryPkg
from mama.types.git import Git
from mama.types.local_source import LocalSource


# every shape the version field can take, so the regression net covers them all
SHAPES = [
    ('a pinned self.version', dict(version='1.2.3')),
    ('a git tag',             dict(version='', git_tag='v1.0.0')),
    ('a branch and a commit', dict(version='', git_branch='main')),
    ('a bare commit',         dict(version='', is_git=True)),
]


@pytest.mark.parametrize('what, kw', SHAPES, ids=[s[0] for s in SHAPES])
def test_an_empty_suffix_leaves_every_archive_name_untouched(what, kw):
    # the compatibility guarantee: nobody who never sets a suffix sees a package name move
    assert _name(**kw) == _name(**kw, version_suffix='')


@pytest.mark.parametrize('what, kw', SHAPES, ids=[s[0] for s in SHAPES])
def test_a_suffix_appends_once_to_every_version_shape(what, kw):
    assert _name(**kw, version_suffix='2') == f'{_name(**kw)}-2'


def test_the_suffix_is_sanitized_like_the_version():
    # it reaches a filename and a url, so a slash or a space must not travel
    name = _name(version='1.0', version_suffix='a b/c')
    assert name.endswith('-1.0-a-b-c') and '/' not in name and ' ' not in name


@pytest.mark.parametrize('source, kw', [(Git, dict(url='u', branch='', tag='', mamafile=None, shallow=True, args=[])),
                                        (LocalSource, dict(rel_path='.', mamafile=None, always_build=False, args=[])),
                                        (ArtifactoryPkg, dict(version='1', fullname=''))])
def test_every_dep_source_carries_the_suffix(source, kw):
    assert source(name='x', **kw).version_suffix == ''
    assert source(name='x', **kw, version_suffix='7').version_suffix == '7'


def test_a_fullname_pkg_refuses_a_suffix(tmp_path):
    # a fullname returns before the suffix applies, so accepting both would drop it without a word
    target = make_package_target(tmp_path, package=None)
    with pytest.raises(RuntimeError, match='cannot take both fullname and version_suffix'):
        target.add_artifactory_pkg('x', fullname='x-linux-x64-release-abc', version_suffix='2')


# --- the papa round trip: a published package must not lose a child's suffix ------------------

def _deployed_papa(tmp_path, suffix):
    """Deploy a target whose one child declares `suffix`, then read the papa.txt back."""
    dep = make_mock_dep(tmp_path, name='parent')
    child = dep.add_child(Git('kid', 'https://example.com/kid.git', 'main', '', None, True, [], suffix))
    target = make_exporting_target(dep, includes=[], libs=[])
    target.children = lambda: [child]
    out = str(tmp_path / 'deploy')
    papa_deploy_to(target, out, r_includes=False, r_dylibs=False, r_syslibs=False, r_assets=False)
    return PapaFileInfo(f'{out}/papa.txt')


def test_a_child_suffix_survives_the_papa_round_trip(tmp_path):
    papa = _deployed_papa(tmp_path, '2')
    assert [d.name for d in papa.dependencies] == ['kid']
    assert papa.dependencies[0].version_suffix == '2'


def test_a_child_with_no_suffix_writes_no_record(tmp_path):
    papa = _deployed_papa(tmp_path, '')
    assert papa.dependencies[0].version_suffix == ''
    assert 'V ' not in open(f'{tmp_path}/deploy/papa.txt').read()
