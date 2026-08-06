from __future__ import annotations
from typing import TYPE_CHECKING
from collections import Counter
import os, zipfile, shutil

from .artifactory import artifactory_archive_name, artifactory_upload_ftp
from .build_names import build_dir_build_type
from .mamafile_version import pinned_version, computed_local_version
from .local_version import is_publishable
from .utils.paths import normalized_join, path_join, forward_slashes
from .utils.progress import get_file_size_str, ProgressBar
from .utils.system import console, error, warning
from .papa_deploy import PapaFileInfo, describe_duplicate_trees, find_duplicate_trees

if TYPE_CHECKING:
    from .build_target import BuildTarget

_CHUNK_SIZE = 1024*1024  # big enough to keep DEFLATE fed, small enough that the bar moves smoothly


def _archive_entries(rel_path:str, full_path:str):
    """(src, rel, size) for one papa record. A dir flattens into its tree, with dir entries kept so the
    zip keeps its structure. size is None for a dir, else read once here and reused as the bar weight."""
    if not os.path.isdir(full_path):
        return [(full_path, rel_path, os.path.getsize(full_path))]
    entries = []
    for full_dir, _, files in os.walk(full_path):
        nested_rel = os.path.relpath(full_dir, full_path)
        rel_dir = rel_path if nested_rel == '.' else f'{rel_path}/{nested_rel}'
        entries.append((full_dir, rel_dir, None)) # dir entry: no payload to compress
        for file in files:
            src_file = f'{full_dir}/{file}'
            entries.append((src_file, f'{rel_dir}/{file}', os.path.getsize(src_file)))
    return entries


def _dedupe_includes(includes:list) -> list:
    """Drops an include record that lies inside another include record. Both records name the same files at
    the same archive path, so the nested record only writes every file a second time. The papa.txt keeps both
    records, because the consumer still adds both include paths."""
    roots = []  # each root keeps a trailing slash, so one prefix test covers both an equal and a nested record
    for include in sorted(includes, key=len):
        nested = include + '/'
        if not any(nested.startswith(root) for root in roots): roots.append(nested)
    return [root[:-1] for root in roots]


def _archive_groups(papa:PapaFileInfo, package_full_path:str):
    """(verbose label, entries) per papa.txt record in write order: manifest, includes, libs, assets."""
    groups = [('', _archive_entries('papa.txt', papa.papa_file))]
    for include in _dedupe_includes(papa.includes):
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


def _archive_total_size(groups:list) -> int:
    """Uncompressed bytes the zip will hold. Drives both the bar and the compression level."""
    return sum(size or 0 for _, entries in groups for _, _, size in entries)


def _compress_level(total:int) -> int:
    """Level 8 buys a couple percent of size for minutes of CPU once a package passes 100MB, because a
    few bloated static libs dominate a PAPA package. Stay at 8 while it is cheap and drop to 6 above."""
    return 6 if total > 100*1024*1024 else 8


def _write_file(zip:zipfile.ZipFile, src:str, rel:str, bar:ProgressBar):
    """Streams one file into the zip so the bar advances DURING a 60MB lib instead of jumping after it.
    from_file mirrors ZipFile.write's metadata, so the exec bit on bin/protoc survives the round trip."""
    zinfo = zipfile.ZipInfo.from_file(src, rel)
    zinfo.compress_type = zip.compression
    zinfo._compresslevel = zip.compresslevel
    with zip.open(zinfo, 'w') as dst, open(src, 'rb') as file:
        while chunk := file.read(_CHUNK_SIZE):
            dst.write(chunk)
            if bar: bar.step(len(chunk), rel)


def _write_archive(zip:zipfile.ZipFile, groups:list, config, indent:str, total:int):
    """Writes every entry into the zip. Verbose keeps its per-record lines. Regular verbosity gets a
    progress bar, because a big package (protobuf ships ~100 libs) otherwise looks frozen for minutes."""
    show_bar = config.print and not config.verbose
    bar = ProgressBar(total, indent) if show_bar else None
    for label, entries in groups:
        if config.verbose and label: console(label)
        for src, rel, size in entries:
            if size is None: zip.write(src, rel)  # dir entry: nothing to stream
            else: _write_file(zip, src, rel, bar)
    if bar: bar.finish()


def _include_files(include:str, package_full_path:str) -> list:
    """(archive path, source path) for every file one include record ships. A file record ships itself."""
    if not os.path.isdir(include):
        return [(forward_slashes(os.path.relpath(include, package_full_path)), include)]
    files = []
    for full_dir, _, names in os.walk(include):
        for name in names:
            src_file = path_join(full_dir, name)
            files.append((forward_slashes(os.path.relpath(src_file, package_full_path)), src_file))
    return files


def validate_archive(package_full_path: str, papa: PapaFileInfo, archive_path: str):
    expected = Counter(['papa.txt'])
    empty_includes = []
    include_files = []

    for include in _dedupe_includes(papa.includes):
        files = _include_files(include, package_full_path)
        # An include record with no file under it ships a package no consumer can include from.
        # The counts below still match, so nothing else catches it.
        if not files: empty_includes.append(forward_slashes(os.path.relpath(include, package_full_path)))
        expected.update(rel for rel, _ in files)
        include_files += files

    if empty_includes:
        raise RuntimeError(f'PAPA archive validation failed for {archive_path}: include dirs hold no files: ' + \
                           f'{empty_includes}. Check the export_include() paths of {papa.project_name}.')

    trees = find_duplicate_trees(include_files)
    if trees:
        raise RuntimeError(f'PAPA archive validation failed for {archive_path}: the include tree holds ' + \
                           f'{len(trees)} duplicated directory pairs.\n{describe_duplicate_trees(trees)}\n' + \
                           f'    Fix the export_include() paths of {papa.project_name}.')

    for lib in papa.libs:
        rel_path = os.path.relpath(lib, package_full_path)
        expected[forward_slashes(rel_path)] += 1

    for asset in papa.assets:
        expected[forward_slashes(asset.outpath)] += 1

    with zipfile.ZipFile(archive_path) as zip:
        actual = Counter(
            forward_slashes(info.filename)
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


def _download_can_find_this_version(target:BuildTarget) -> bool:
    """True when the version this upload names is the one a DOWNLOAD would look for.
    An upload runs the mamafile and uses the value in memory. A download reads the file as text, because
    it must name the package before the clone. A computed or twice-assigned version makes the two disagree,
    and mama would publish an archive no consumer can ever ask for. Refuse the upload instead."""
    dep = target.dep
    executed = target.version or ''
    if dep.dep_source.is_src and not dep.is_root:
        return _local_module_can_publish(target, executed)
    readable = pinned_version(dep)
    if executed == readable: return True
    error(f'  - Target {target.name: <16} UPLOAD REFUSED: this build named the package ' +
          f'{executed or "<commit hash>"!r}, but a download reads {readable or "<commit hash>"!r} ' +
          'from the mamafile. Pin self.version with ONE raw string literal, or drop it.')
    return False


def _local_module_can_publish(target:BuildTarget, executed:str) -> bool:
    """A local module needs no pre-clone reader, because its source is on disk for both sides. So the
    text-scan rule does not apply, and a mamafile may compute its own version.

    Only one thing stops the upload: an uncommitted edit. No other machine can reproduce that tree, so
    the package would carry a name that means one thing on one disk. The build still finishes, the same
    way a 404 for a git dep is not fatal."""
    if is_publishable(target.dep): return True
    named = executed or computed_local_version(target.dep) or '<computed>'
    warning(f'  - Target {target.name: <16} UPLOAD SKIPPED: {named} names an edited working tree, ' +
            'so no other machine can rebuild it. Commit the changes to publish this package.')
    return False


def papa_upload_to(target:BuildTarget, package_full_path:str):
    """Archives the deployed PAPA package, validates it, and uploads it to the artifactory server.
    - target: the configured and packaged target
    - package_full_path: full path to the deployed PAPA package
    """
    if not _download_can_find_this_version(target):
        return
    package_full_path = package_full_path if package_full_path else target.build_dir()
    papa_file = normalized_join(package_full_path, 'papa.txt')
    if not os.path.exists(papa_file):
        raise RuntimeError(f'BuildTarget {target.name} was not deployed because {papa_file} does not exist!' + \
                           ' Add self.papa_deploy() to deploy(), or self.nothing_to_upload() to settings().')

    config = target.config
    built = build_dir_build_type(target.dep)
    archive_name = artifactory_archive_name(target, build_type=built)
    if not archive_name:
        raise Exception(f'Could not get archive name for target: {target.name}')

    # The dir holds the artifacts of its last build, whatever type this run asks for. Say so, because
    # the name follows the artifacts and a reader expects the flag of the run.
    if built and built != ('release' if config.release else 'debug'):
        warning(f'  - Target {target.name: <16} UPLOAD: the build dir holds a {built} build, so the archive is {built}.')

    archive_path = target.build_dir(archive_name + '.zip')
    if config.verbose:
        console(f'    archiving {papa_file}\n {"":10}-> {archive_path}')

    # build the archive by hand, so it holds only the files papa.txt names
    papa = PapaFileInfo(papa_file)
    temp_archive = archive_path + '.tmp'
    groups = _archive_groups(papa, package_full_path)
    total = _archive_total_size(groups)
    level = _compress_level(total)
    with zipfile.ZipFile(temp_archive, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=level) as zip:
        if config.verbose: console(f'      root {package_full_path} ({get_file_size_str(total)}, deflate {level})')
        _write_archive(zip, groups, config, f'  - {target.name: <16} ', total)

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
