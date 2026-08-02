"""Pins that a sanitizer or coverage build fetches its artifactory package into the variant build dir
and that a nested include record survives the deploy, archive and fetch round trip."""
import os, shutil, zipfile
from unittest.mock import patch

import pytest
from testutils import make_mock_dep

from mama import artifactory, papa_deploy, papa_upload
from mama.build_target import BuildTarget


def _make_target(dep, includes, libs):
    target = BuildTarget(name=dep.name, config=dep.config, dep=dep, args=[])
    target.version = 'abc1234'
    target.exported_includes = includes
    target.exported_libs = libs
    return target


def _deploy_and_archive(tmp_path, target, package_path):
    """papa_deploy + the archive papa_upload_to would build, minus the FTP transfer."""
    with patch.object(BuildTarget, 'children', lambda self: []):
        papa_deploy.papa_deploy_to(target, package_path, r_includes=False, r_dylibs=False,
                                   r_syslibs=False, r_assets=False)
    papa = papa_deploy.PapaFileInfo(os.path.join(package_path, 'papa.txt'))
    archive = str(tmp_path / 'package.zip')
    with zipfile.ZipFile(archive, 'w') as zip:
        for _, entries in papa_upload._archive_groups(papa, package_path):
            for src, rel, _ in entries: zip.write(src, rel)
    papa_upload.validate_archive(package_path, papa, archive)
    return archive


def _publish(tmp_path, sanitize=None, include_rel='include', **overrides):
    """Build `include_rel` and a lib in a producer build dir, deploy them, and return the archive."""
    dep = make_mock_dep(tmp_path / 'producer', name='libfoo', sanitize=sanitize, **overrides)
    build = dep.build_dir
    os.makedirs(f'{build}/{include_rel}/foo', exist_ok=True)
    open(f'{build}/{include_rel}/foo/foo.h', 'w').write('#pragma once\n')
    os.makedirs(f'{build}/lib', exist_ok=True)
    open(f'{build}/lib/libfoo.a', 'wb').write(b'\0' * 8)
    target = _make_target(dep, [f'{build}/{include_rel}'], [f'{build}/lib/libfoo.a'])
    return _deploy_and_archive(tmp_path, target, f'{build}/deploy/libfoo')


def _fetch(tmp_path, archive, sanitize=None, **overrides):
    """Fetch `archive` as the artifactory package of a fresh consumer dep. Returns (dep, target)."""
    dep = make_mock_dep(tmp_path / 'consumer', name='libfoo', sanitize=sanitize, **overrides)
    target = BuildTarget(name=dep.name, config=dep.config, dep=dep, args=[])
    target.version = 'abc1234'
    copy = lambda t, url, name, cache: shutil.copy(archive, os.path.join(cache, f'{name}.zip'))
    with patch('mama.artifactory._fetch_package', side_effect=copy):
        fetched, _ = artifactory.artifactory_fetch_and_reconfigure(target)
    assert fetched
    return dep, target


@pytest.mark.parametrize('sanitize,dir_name', [(None, 'linux'), ('address', 'linux-asan'), ('thread', 'linux-tsan')])
def test_package_lands_in_the_variant_build_dir(tmp_path, sanitize, dir_name):
    dep, target = _fetch(tmp_path, _publish(tmp_path, sanitize), sanitize)
    assert dep.build_dir.endswith(f'/libfoo/{dir_name}')
    assert os.path.exists(f'{dep.build_dir}/include/foo/foo.h')
    assert target.exported_includes == [f'{dep.build_dir}/include']
    assert target.exported_libs == [f'{dep.build_dir}/lib/libfoo.a']


def test_a_coverage_build_gets_its_own_dir_and_archive(tmp_path):
    dep, _ = _fetch(tmp_path, _publish(tmp_path, 'address', coverage='default'), 'address', coverage='default')
    assert dep.build_dir.endswith('/libfoo/linux-cov-asan')


def test_a_sanitizer_fetch_leaves_the_plain_build_dir_alone(tmp_path):
    plain, _ = _fetch(tmp_path, _publish(tmp_path, include_rel='include'), None)
    open(f'{plain.build_dir}/include/foo/marker.h', 'w').write('// plain build\n')
    asan, target = _fetch(tmp_path, _publish(tmp_path, 'address'), 'address')
    assert asan.build_dir != plain.build_dir
    assert not os.path.exists(f'{asan.build_dir}/include/foo/marker.h')  # the asan tree is its own
    assert os.path.exists(f'{plain.build_dir}/include/foo/marker.h')


def test_two_sanitizers_cache_their_archives_apart(tmp_path):
    asan, _ = _fetch(tmp_path, _publish(tmp_path, 'address'), 'address')
    tsan, _ = _fetch(tmp_path, _publish(tmp_path, 'thread'), 'thread')
    assert asan.dep_dir == tsan.dep_dir  # same dep, so the cached zips share one directory
    zips = sorted(f for f in os.listdir(asan.dep_dir) if f.endswith('.zip'))
    assert zips == ['libfoo-ubuntu-22-gcc11.3-x64-release-asan-abc1234.zip',
                    'libfoo-ubuntu-22-gcc11.3-x64-release-tsan-abc1234.zip']


def test_a_nested_include_dir_survives_the_round_trip(tmp_path):
    # the opencv shape: export_include('include/opencv4'), so the consumer must get the nested dir back
    dep, target = _fetch(tmp_path, _publish(tmp_path, 'address', include_rel='include/opencv4'), 'address')
    assert target.exported_includes == [f'{dep.build_dir}/include/opencv4']
    assert os.path.exists(f'{dep.build_dir}/include/opencv4/foo/foo.h')


def test_an_include_dir_with_no_files_fails_the_upload(tmp_path):
    dep = make_mock_dep(tmp_path / 'producer', name='libfoo', sanitize='address')
    build = dep.build_dir
    os.makedirs(f'{build}/include/empty', exist_ok=True)  # dirs only: every header filtered out or never built
    os.makedirs(f'{build}/lib', exist_ok=True)
    open(f'{build}/lib/libfoo.a', 'wb').write(b'\0' * 8)
    target = _make_target(dep, [f'{build}/include'], [f'{build}/lib/libfoo.a'])
    with pytest.raises(RuntimeError, match='include dirs hold no files'):
        _deploy_and_archive(tmp_path, target, f'{build}/deploy/libfoo')
