from __future__ import annotations
from typing import TYPE_CHECKING
from collections import Counter
import os, zipfile, shutil

from .artifactory import artifactory_archive_name, artifactory_upload_ftp
from .util import get_file_size_str, console, normalized_join, ProgressBar
from .papa_deploy import PapaFileInfo

if TYPE_CHECKING:
    from .build_target import BuildTarget


def _archive_entries(rel_path:str, full_path:str):
    """(src, rel, size) for one papa record; a dir flattens into its tree, dir entries included so the
    zip keeps its structure. Size is stat'd once here and reused as the progress weight."""
    if not os.path.isdir(full_path):
        return [(full_path, rel_path, os.path.getsize(full_path))]
    entries = []
    for full_dir, _, files in os.walk(full_path):
        nested_rel = os.path.relpath(full_dir, full_path)
        rel_dir = rel_path if nested_rel == '.' else f'{rel_path}/{nested_rel}'
        entries.append((full_dir, rel_dir, 0)) # dir entry: no payload to compress
        for file in files:
            src_file = f'{full_dir}/{file}'
            entries.append((src_file, f'{rel_dir}/{file}', os.path.getsize(src_file)))
    return entries


def _archive_groups(papa:PapaFileInfo, package_full_path:str):
    """(verbose label, entries) per papa.txt record in write order: manifest, includes, libs, assets."""
    groups = [('', _archive_entries('papa.txt', papa.papa_file))]
    for include in papa.includes:
        rel_path = os.path.relpath(include, package_full_path)
        groups.append((f'      adding {rel_path} {include}', _archive_entries(rel_path, include)))
    for lib in papa.libs:
        rel_path = os.path.relpath(lib, package_full_path)
        if rel_path.startswith('..'):
            raise Exception(f'lib path {lib} is outside of the package path {package_full_path}')
        groups.append((f'      adding {rel_path} {lib}', _archive_entries(rel_path, lib)))
    for asset in papa.assets:
        groups.append((f'      adding {asset.outpath} {asset}', _archive_entries(asset.outpath, asset.srcpath)))
    return groups


def _write_archive(zip:zipfile.ZipFile, groups:list, config, indent:str):
    """Writes every entry into the zip. Verbose keeps its per-record lines; regular verbosity gets a
    progress bar, since a big package (protobuf ships ~100 libs) otherwise looks frozen for minutes."""
    show_bar = config.print and not config.verbose
    bar = ProgressBar(sum(size for _, e in groups for _, _, size in e), indent) if show_bar else None
    for label, entries in groups:
        if config.verbose and label: console(label)
        for src, rel, size in entries:
            zip.write(src, rel)
            if bar: bar.step(size)  # per file, so one huge include dir still moves the bar
    if bar: bar.finish()


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
    groups = _archive_groups(papa, package_full_path)
    with zipfile.ZipFile(temp_archive, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=8) as zip:
        if config.verbose: console(f'      root {package_full_path}')
        _write_archive(zip, groups, config, f'  - {target.name: <16} ')

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
