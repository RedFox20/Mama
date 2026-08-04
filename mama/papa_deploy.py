from __future__ import annotations
from typing import List, TYPE_CHECKING
import os, itertools

from .types.git import Git
from .types.local_source import LocalSource
from .types.artifactory_pkg import ArtifactoryPkg
from .types.dep_source import DepSource
from .types.asset import Asset

from .util import normalized_join, path_join, read_lines_from, forward_slashes, file_sha1 \
                , write_text_to, console, copy_if_needed, copy_dir, has_shim_marker
from .utils.system import warning, System

import mama.package as package

if TYPE_CHECKING:
    from .build_target import BuildTarget
    from .build_dependency import BuildDependency


def _gather_dependencies(target:BuildTarget) -> List[BuildDependency]:
    dependecies = []
    for child in target.children():
        dependecies.append(child)
    return dependecies


def _results_contain(results, contains_value):
    for target,value in results:
        if value == contains_value:
            return True
    return False


def _gather(target:BuildTarget, recurse, results:list, get_candidates):
    for value in get_candidates(target):
        if not _results_contain(results, value):
            results.append((target,value))
    if recurse:
        for child in target.children():
            _gather(child.target, True, results, get_candidates)
    return results


def _gather_includes(target:BuildTarget, recurse):
    includes = []
    return _gather(target, recurse, includes, lambda t: t.exported_includes)


def _gather_libs(target:BuildTarget, recurse):
    libs = [(target,l) for l in target.exported_libs]

    if recurse:
        def get_dylibs(t:BuildTarget):
            for l in t.exported_libs:
                if package.is_a_dynamic_library(l): yield l
        for child in target.children():
            _gather(child.target, recurse, libs, get_dylibs)
    return libs


def _gather_syslibs(target:BuildTarget, recurse):
    syslibs = []
    return _gather(target, recurse, syslibs, lambda t: t.exported_syslibs)


def _gather_assets(target:BuildTarget, recurse):
    assets = []
    return _gather(target, recurse, assets, lambda t: t.exported_assets)


def _header_stems(includes:list, suffixes:tuple) -> set:
    """Lowercased name of every real header in the exported trees, `qcorotask` for qcorotask.h."""
    stems = set()
    for _, abs_include in includes:
        for _, _, names in os.walk(abs_include):
            stems |= {os.path.splitext(n)[0].lower() for n in names if n.endswith(suffixes)}
    return stems


def _include_deploy(target:BuildTarget, includes_root:str, abs_include:str):
    """(source dir, deployed dir, papa record) for one exported include dir. The record is the include
    path a consumer gets, so `as_includes_root` deploys src/mylib as include/mylib and records include."""
    root_path, root_src, alias = target.includes_root
    if root_path and abs_include == root_path:
        return f'{abs_include}/{os.path.basename(root_src)}', f'{includes_root}/{alias}', 'I include'
    name = os.path.basename(abs_include)
    if name == 'include': return abs_include, includes_root, 'I include'
    return abs_include, f'{includes_root}/{name}', f'I include/{name}'


def _append_includes(target:BuildTarget, package_full_path, detail_echo, descr, includes):
    if not includes:
        return # nothing to do
    config = target.config
    includes_root = package_full_path + '/include' # output root
    # TODO: should we include .cpp files for easier debugging?
    suffixes = tuple(target.include_glob_filter)
    stems = _header_stems(includes, suffixes)

    def is_header(path:str) -> bool:
        name = os.path.basename(path)
        if name.endswith(suffixes): return True
        # Qt-style stub headers carry no extension (`#include <QCoro/QCoroTask>`). Ship one only when
        # the header it forwards to is in the tree, so a LICENSE or an AUTHORS file never ships.
        return '.' not in name and name.lower() in stems

    # Two exported dirs whose names differ only by case, such as QCoro/ next to qcoro/, follow the
    # filesystem. Windows and macOS hold ONE dir for the pair, so the deploy merges them and records the
    # pair once. Two records there would zip the same files twice and fail the upload. Linux keeps both
    # dirs: QCoro includes "qcorotask.h" in one header and "qcoro/coroutine.h" in the next, and only the
    # split resolves both forms.
    merge_variants = System.windows or System.macos
    exported = []  # export dir names already handled, so one name never ships twice
    deploy_dirs = {}  # lowercased deploy dir name -> the deploy dir that shipped first
    for inctarget, abs_include in includes:
        name = os.path.basename(abs_include)
        if name in exported: continue
        exported.append(name)
        src_dir, dst_dir, record = _include_deploy(target, includes_root, abs_include)
        first = dst_dir
        if merge_variants: first = deploy_dirs.setdefault(os.path.basename(dst_dir).lower(), dst_dir)
        if first != dst_dir:
            dst_dir = first  # merged: the record of the dir that shipped first already names this one
        else:
            descr.append(record)
            if detail_echo: console(f'    I ({inctarget.name+")": <16}  {record[2:]}')
        if src_dir != dst_dir:
            if config.verbose: console(f'    copy {src_dir}\n      -> {dst_dir}')
            copy_dir(src_dir, dst_dir, is_header, remap_root_dirname=True)


def find_duplicate_trees(files:list) -> list:
    """(dir a, dir b, [file names]) for every pair of directories that hold the same file content twice.
    A project that installs its headers into two include dirs doubles the archive and gives the consumer
    two copies of every header.
    - files: (relative path, full path) for every file to compare"""
    by_name = {}
    for rel, full in files:
        by_name.setdefault((os.path.basename(rel), os.path.getsize(full)), []).append((rel, full))
    pairs = {}
    for group in by_name.values():
        if len(group) < 2: continue  # a unique name and size cannot collide with anything
        by_hash = {}
        for rel, full in group:
            by_hash.setdefault(file_sha1(full), []).append(rel)
        for rels in by_hash.values():
            for first, second in itertools.combinations(sorted(rels), 2):
                dir_a, dir_b = os.path.dirname(first), os.path.dirname(second)
                # two identical files in ONE dir are two names for one header, not a duplicated tree
                if dir_a != dir_b: pairs.setdefault((dir_a, dir_b), []).append(os.path.basename(first))
    return [(dir_a, dir_b, sorted(names)) for (dir_a, dir_b), names in sorted(pairs.items())]


def describe_duplicate_trees(trees:list, indent:str='    ') -> str:
    """One line per duplicated directory pair, with up to 3 file names as examples."""
    lines = []
    for dir_a, dir_b, names in trees[:5]:
        examples = ', '.join(names[:3]) + (', ...' if len(names) > 3 else '')
        lines.append(f'{indent}{dir_a} and {dir_b} hold {len(names)} identical files: {examples}')
    if len(trees) > 5: lines.append(f'{indent}and {len(trees) - 5} more directory pairs')
    return '\n'.join(lines)


def _deployed_include_files(package_full_path:str) -> list:
    """(relative path, full path) for every file under the deployed include root."""
    files = []
    for full_dir, _, names in os.walk(f'{package_full_path}/include'):
        rel_dir = forward_slashes(os.path.relpath(full_dir, package_full_path))
        files += [(f'{rel_dir}/{name}', path_join(full_dir, name)) for name in names]
    return files


def _warn_about_duplicate_include_trees(target:BuildTarget, package_full_path:str):
    """Report a duplicated header tree while the deploy still shows what it copied. The upload refuses
    such a package, so this warning names the fix at build time."""
    trees = find_duplicate_trees(_deployed_include_files(package_full_path))
    if trees:
        warning(f'  PAPA Deploy {target.name}: the include tree holds {len(trees)} duplicated directory ' + \
                f'pairs.\n{describe_duplicate_trees(trees)}\n    Fix the export_include() paths of {target.name}.')


def _compiler_stamp(config) -> str:
    """'gcc14.3' / 'clang18.1', or '' when the platform cannot name one. A diagnostic, never a deploy failure."""
    try: return config.compiler_version()
    except Exception: return ''


def papa_deploy_to(target:BuildTarget, package_full_path:str,
                   r_includes:bool, r_dylibs:bool,
                   r_syslibs:bool, r_assets:bool):
    config = target.config
    detail_echo = config.print and target.is_current_target() and (not config.test)
    if detail_echo: console(f'  - PAPA Deploy {package_full_path}')

    # Defense-in-depth: never write into a directory holding a shim marker. A misconfigured caller could
    # pass the shim's build_dir and corrupt the artifactory snapshot. The deploy-skip lives in _execute_deploy_tasks.
    if has_shim_marker(package_full_path):
        raise RuntimeError(f'papa_deploy refused: {package_full_path} contains a mama_shim marker.')

    dependencies = _gather_dependencies(target)

    if not os.path.exists(package_full_path): # check to avoid Access Denied errors
        os.makedirs(package_full_path, exist_ok=True)

    # `C` records the compiler that built these libs, the same string as in the archive name,
    # so a load can spot a gcc tree in a clang build.
    descr = [ f'P {target.name}' ]
    compiler = _compiler_stamp(config)
    if compiler: descr.append(f'C {compiler}')
    for d in dependencies:
        if detail_echo: console(f'    D {d.dep_source}')
        descr.append(f'D {d.dep_source.get_papa_string()}')

    includes = _gather_includes(target, r_includes)
    _append_includes(target, package_full_path, detail_echo, descr, includes)
    _warn_about_duplicate_include_trees(target, package_full_path)

    build_dir = target.build_dir()
    source_dir = target.source_dir()

    libs = _gather_libs(target, r_dylibs)
    for libtarget, lib in libs:
        if   lib.startswith(build_dir):  relpath = os.path.relpath(lib, build_dir)
        elif lib.startswith(source_dir): relpath = os.path.relpath(lib, source_dir)
        else: relpath = lib
        descr.append(f'L {relpath}')
        outpath = normalized_join(package_full_path, relpath)
        os.makedirs(os.path.dirname(outpath), exist_ok=True)
        if detail_echo: console(f'    L ({libtarget.name+")": <16}  {relpath}')
        if lib != outpath:
            if config.verbose: console(f'    copy {lib}\n      -> {outpath}')
            copy_if_needed(lib, outpath)

    syslibs = _gather_syslibs(target, r_syslibs)
    for systarget, syslib in syslibs:
        syslib_basename = package.get_lib_basename(syslib)
        descr.append(f'S {syslib_basename}')
        if detail_echo: console(f'    S ({systarget.name+")": <16}  {syslib_basename}')

    assets = _gather_assets(target, r_assets)
    for asstarget, asset in assets:
        descr.append(f'A {asset.outpath}')
        if detail_echo: console(f'    A ({asstarget.name+")": <16}  {asset.outpath}')
        outpath = normalized_join(package_full_path, asset.outpath)
        if asset.srcpath != outpath:
            folder = os.path.dirname(outpath)
            if not os.path.exists(folder):
                os.makedirs(folder, exist_ok=True)
            copy_if_needed(asset.srcpath, outpath)

    write_text_to(os.path.join(package_full_path, 'papa.txt'), '\n'.join(descr))

    if config.print:
        console(f'  PAPA Deployed: {len(includes)} includes, {len(libs)} libs, {len(syslibs)} syslibs, {len(assets)} assets')


def make_dep_source(s:str) -> DepSource:
    if s.startswith('git '): return Git.from_papa_string(s[4:])
    if s.startswith('pkg '): return ArtifactoryPkg.from_papa_string(s[4:])
    if s.startswith('src '): return LocalSource.from_papa_string(s[4:])
    raise RuntimeError(f'Unrecognized dependency source: {s}')


class PapaFileInfo:
    def __init__(self, papa_file:str):
        if not os.path.exists(papa_file):
            raise FileNotFoundError(f'Package file not found: {papa_file}')
        self.papa_file = papa_file
        self.papa_dir = os.path.dirname(papa_file)

        self.project_name = None
        self.compiler = None # 'gcc14.3' / 'clang18.1'. None for a package that predates the C record
        self.dependencies = []
        self.includes = []
        self.libs = []
        self.syslibs = []
        self.assets: List[Asset] = []

        def append_to(to:list, line):
            to.append(normalized_join(self.papa_dir, line[2:].strip()))

        for line in read_lines_from(self.papa_file):
            if   line.startswith('P '): self.project_name = line[2:].strip()
            elif line.startswith('C '): self.compiler = line[2:].strip()
            elif line.startswith('D '): self.dependencies.append(make_dep_source(line[2:].strip()))
            elif line.startswith('I '): append_to(self.includes, line)
            elif line.startswith('L '): append_to(self.libs, line)
            elif line.startswith('S '): append_to(self.syslibs, line)
            elif line.startswith('A '):
                relpath = line[2:].strip()
                fullpath = normalized_join(self.papa_dir, relpath)
                self.assets.append(Asset(relpath, fullpath, None))

