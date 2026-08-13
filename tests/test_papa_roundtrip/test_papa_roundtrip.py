"""Pins the papa round trip: deploying a fetched archive must produce what the source build deployed."""
import os, shutil
import pytest

from testutils import make_package_target

from mama.artifactory import artifactory_load_target
from mama.utils.archive import try_unzip

# A build output with payload suffixes a plain header filter would drop.
BUILD_FILES = ['include/foo/foo.h', 'include/foo/foo.hpp', 'include/foo/detail.inc',
               'include/foo/table.txt', 'include/foo/readme.md',
               'include/foo/foo.cppm',
               'src/api.h', 'src/detail.inc', 'lib/libfoo.a', 'bin/tool']


SOURCE_FILES = ['data/table.txt', 'data/params.xml', 'notes.md']


def _write_source_tree(src_dir):
    for rel in SOURCE_FILES:
        path = os.path.join(src_dir, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as f: f.write(f'; {rel}\n')


def _write_build_output(build_dir):
    for rel in BUILD_FILES:
        path = os.path.join(build_dir, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as f: f.write(f'// {rel}\n')


def _tree(root):
    """Every file under `root`, relative and sorted, so two deploys compare directly."""
    return sorted(os.path.relpath(os.path.join(d, f), root).replace('\\', '/')
                  for d, _, files in os.walk(root) for f in files)


# --- the export styles under test -------------------------------------------

def _default(self):
    self.export_include('include', build_dir=True)

def _filter_inc(self):
    self.export_include('include', build_dir=True, includes_filter=['.h', '.hpp', '.inc'])

def _filter_txt(self):
    # a recipe may ship a data table beside its headers, and it means it
    self.export_include('include', build_dir=True, includes_filter=['.h', '.txt'])

def _includes_root(self):
    # the documented shape: export a source dir that only a checkout has, rooted under its own name
    self.export_include('src', build_dir=True, as_includes_root='foo', includes_filter=['.h', '.inc'])

def _libs_only(self):
    self.no_export_includes()
    self.export_libs('lib', ['.a'], build_dir=True)

def _syslibs(self):
    self.export_include('include', build_dir=True)
    self.export_syslib('dl', required=False)

def _assets(self):
    self.export_include('include', build_dir=True)
    self.export_asset('bin/tool', build_dir=True)

def _source_root_payload(self):
    # the shape ArduPilotParams and sdl_gamecontrollerdb use: ship data files, not headers
    self.export_include('', build_dir=False, includes_filter=['.txt', '.xml'])

def _root_over_include(self):
    # the pathological shape: the rooted dir name is one the archive also has
    self.export_include('include', build_dir=True, as_includes_root='foo', includes_filter=['.h', '.inc'])

def _modules(self):
    # a fetched package must keep its M records, so the 5th export category survives the reload
    self.export_include('include', build_dir=True)
    # strip_objects=False: this fixture writes a stub lib, and a real archiver cannot read it
    self.export_modules('include/foo', ['foo.cppm'], build_dir=True, strip_objects=False)

def _everything(self):
    self.export_include('include', build_dir=True, includes_filter=['.h', '.hpp', '.inc', '.txt'])
    self.export_libs('lib', ['.a'], build_dir=True)
    self.export_syslib('dl', required=False)
    self.export_asset('bin/tool', build_dir=True)

STYLES = {'default': _default, 'filter_inc': _filter_inc, 'filter_txt': _filter_txt,
          'source_root_payload': _source_root_payload, 'root_over_include': _root_over_include,
          'includes_root': _includes_root, 'libs_only': _libs_only, 'syslibs': _syslibs,
          'assets': _assets, 'modules': _modules, 'everything': _everything}


def _deploy(root, recipe, *, fetched_from=None, shape='shim', source_of=None):
    """Package and deploy one target under `root`. `fetched_from` unzips an archive first, so the
    target loads the way an artifactory fetch leaves it. `shape` picks which fetched shape to model:
    a shim has no working tree, a fetched clone still has its source. Returns (deploy dir, the target)."""
    root.mkdir(parents=True, exist_ok=True)
    target = make_package_target(root, package=recipe, print=False,
                                 dep_attrs={'should_rebuild': fetched_from is None,
                                            'has_usable_artifacts': lambda: True,
                                            'artifactory_archive': 'libfoo-linux-x64-release-abc1234'})
    build_dir = target.dep.build_dir
    if fetched_from:
        if shape == 'shim': target.dep.src_dir = str(root / 'no_source_here')
        elif source_of:     _write_source_tree(target.dep.src_dir)
        assert try_unzip(fetched_from, build_dir)[0]
        assert artifactory_load_target(target, build_dir, num_files_copied=0)[0]
        assert target.dep.from_artifactory
    else:
        _write_build_output(build_dir)
        _write_source_tree(target.dep.src_dir)
    target._run_packaging()
    target.papa_deploy('pkg')
    return target.papa_path, target


def _archive(deploy_dir, dest_base):
    return shutil.make_archive(str(dest_base), 'zip', root_dir=deploy_dir)


@pytest.mark.parametrize('shape', ['shim', 'clone'])
@pytest.mark.parametrize('style', sorted(STYLES), ids=sorted(STYLES))
def test_a_fetched_archive_deploys_exactly_what_the_source_build_deployed(tmp_path, style, shape):
    recipe = STYLES[style]
    built, _ = _deploy(tmp_path / 'src', recipe)
    archive = _archive(built, tmp_path / 'libfoo-linux-x64-release-abc1234')
    fetched, _ = _deploy(tmp_path / 'pkg', recipe, fetched_from=archive, shape=shape, source_of=True)

    assert _tree(fetched) == _tree(built)
    assert (open(os.path.join(fetched, 'papa.txt')).read()
            == open(os.path.join(built, 'papa.txt')).read())


@pytest.mark.parametrize('style', sorted(STYLES), ids=sorted(STYLES))
def test_the_round_trip_is_stable_over_a_second_reload(tmp_path, style):
    # a package rebuilt from its own archive must not shrink each time it is republished
    recipe = STYLES[style]
    built, _ = _deploy(tmp_path / 'src', recipe)
    once, _ = _deploy(tmp_path / 'pkg1', recipe, fetched_from=_archive(built, tmp_path / 'a1'))
    twice, _ = _deploy(tmp_path / 'pkg2', recipe, fetched_from=_archive(once, tmp_path / 'a2'))
    assert _tree(twice) == _tree(built)


def test_a_payload_suffix_reaches_the_deployed_tree(tmp_path):
    # the whole point of includes_filter: a recipe that asks for .inc and .txt gets them
    built, _ = _deploy(tmp_path / 'src', _everything)
    names = [os.path.basename(p) for p in _tree(built)]
    assert 'detail.inc' in names and 'table.txt' in names
    assert 'readme.md' not in names  # and nothing it did not ask for


def test_as_includes_root_over_a_dir_the_archive_also_has_is_idempotent(tmp_path):
    # an unpacked archive is already rooted, so rooting it again would nest it once per republish
    built, _ = _deploy(tmp_path / 'src', _root_over_include)
    once = _deploy(tmp_path / 'p1', _root_over_include, fetched_from=_archive(built, tmp_path / 'a1'))[0]
    twice = _deploy(tmp_path / 'p2', _root_over_include, fetched_from=_archive(once, tmp_path / 'a2'))[0]
    assert _tree(once) == _tree(built)
    assert _tree(twice) == _tree(built)  # and it stays stable over any number of republishes


def test_a_source_build_still_roots_its_include_under_the_alias(tmp_path):
    # only an unpacked archive skips the re-root. A real build must still get the alias layout,
    # or the round trip would agree with itself while shipping the wrong tree.
    built, _ = _deploy(tmp_path / 'src', _root_over_include)
    assert 'include/foo/foo/foo.h' in _tree(built)
