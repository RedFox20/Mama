from __future__ import annotations
from typing import TYPE_CHECKING
from collections import Counter
import os, zipfile, shutil

from .artifactory import artifactory_archive_name, artifactory_upload_ftp
from .util import get_file_size_str, console, normalized_join
from .papa_deploy import PapaFileInfo

if TYPE_CHECKING:
    from .build_target import BuildTarget


def _append_files_recursive(zip: zipfile.ZipFile, rel_path:str, full_path:str):
    # add each file recursively
    if os.path.isdir(full_path):
        for full_dir, _, files in os.walk(full_path):
            nested_rel = os.path.relpath(full_dir, full_path)
            rel_dir = rel_path if nested_rel == '.' else f'{rel_path}/{nested_rel}'
            # write the directory into the zip as well
            zip.write(full_dir, rel_dir)
            for file in files:
                src_file = full_dir + '/' + file
                rel_file = rel_dir + '/' + file
                #print(f'src_file:{src_file} rel_file:{rel_file}')
                zip.write(src_file, rel_file)
    else:
        zip.write(full_path, rel_path)


def _zip_path(path: str):
    return path.replace('\\', '/')


def validate_archive(package_full_path: str, papa: PapaFileInfo, archive_path: str):
    expected = Counter(['papa.txt'])

    for include in papa.includes:
        if os.path.isdir(include):
            for full_dir, _, files in os.walk(include):
                for file in files:
                    src_file = os.path.join(full_dir, file)
                    rel_file = os.path.relpath(src_file, package_full_path)
                    expected[_zip_path(rel_file)] += 1
        else:
            rel_path = os.path.relpath(include, package_full_path)
            expected[_zip_path(rel_path)] += 1

    for lib in papa.libs:
        rel_path = os.path.relpath(lib, package_full_path)
        expected[_zip_path(rel_path)] += 1

    for asset in papa.assets:
        expected[_zip_path(asset.outpath)] += 1

    with zipfile.ZipFile(archive_path) as zip:
        actual = Counter(
            _zip_path(info.filename)
            for info in zip.infolist()
            if not info.is_dir()
        )

    missing = sorted((expected - actual).elements())
    unexpected = sorted((actual - expected).elements())
    if missing or unexpected:
        preview_missing = missing[:20]
        preview_unexpected = unexpected[:20]
        raise RuntimeError(
            f'PAPA archive validation failed for {archive_path}\n'
            f'missing={preview_missing}\n'
            f'unexpected={preview_unexpected}'
        )


def papa_upload_to(target:BuildTarget, package_full_path:str):
    """
    - target: Target which was configured and packaged
    - package_full_path: Full path to deployed PAPA package
    """
    package_full_path = package_full_path if package_full_path else target.build_dir()
    papa_file = normalized_join(package_full_path, 'papa.txt')
    if not os.path.exists(papa_file):
        raise RuntimeError(f'BuildTarget {target.name} was not deployed because '\
                           f'{package_full_path} does not exist! '\
                            'Add self.papa_deploy() to mamafile deploy()!')

    config = target.config
    archive_name = artifactory_archive_name(target)
    if not archive_name:
        raise Exception(f'Could not get archive name for target: {target.name}')

    archive_path = target.build_dir(archive_name + '.zip')
    if config.verbose:
        console(f'    archiving {papa_file}\n {"":10}-> {archive_path}')

    # archive needs to be created manually to only include the files in papa.txt
    papa = PapaFileInfo(papa_file)
    # create a zip archive with papa.includes, papa.libs and papa.assets
    temp_archive = archive_path + '.tmp'
    with zipfile.ZipFile(temp_archive, 'w',
                         compression=zipfile.ZIP_DEFLATED,
                         compresslevel=8) as zip:
        if config.verbose: console(f'      root {package_full_path}')
        zip.write(papa_file, 'papa.txt') # always add the main manifest file
        for include in papa.includes:
            rel_path = os.path.relpath(include, package_full_path)
            if config.verbose: console(f'      adding {rel_path} {include}')
            _append_files_recursive(zip, rel_path, include)
        for lib in papa.libs:
            rel_path = os.path.relpath(lib, package_full_path)
            if config.verbose: console(f'      adding {rel_path} {lib}')
            if rel_path.startswith('..'):
                raise Exception(f'lib path {lib} is outside of the package path {package_full_path}')
            zip.write(lib, rel_path)
        for asset in papa.assets:
            rel_path = asset.outpath
            if config.verbose: console(f'      adding {rel_path} {asset}')
            zip.write(asset.srcpath, asset.outpath)

    # move the intermediate archive to the final location
    if os.path.exists(archive_path):
        os.remove(archive_path)
    shutil.move(temp_archive, archive_path)
    validate_archive(package_full_path, papa, archive_path)

    if config.print:
        size = os.path.getsize(archive_path)
        console(f'  - PAPA Upload {archive_name}  {get_file_size_str(size)}')

    if artifactory_upload_ftp(target, archive_path):
        if config.verbose:
            console(f'  PAPA Uploaded {os.path.basename(archive_path)}')
