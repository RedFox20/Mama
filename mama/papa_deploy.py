from __future__ import annotations
from typing import List, TYPE_CHECKING
import os, itertools

from .types.git import Git
from .types.local_source import LocalSource
from .types.artifactory_pkg import ArtifactoryPkg
from .types.dep_source import DepSource
from .types.asset import Asset

from .utils.fileio import (read_lines_from, file_sha1, write_text_to, copy_if_needed, copy_dir,
                           remove_tree, _VCS_DIRS)
from .utils.paths import normalized_join, path_join, forward_slashes, has_shim_marker
from .utils.system import console
from .utils.system import warning, System

import mama.package as package
from . import build_names

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


def _gather_modules(target:BuildTarget):
    """The modules of this target alone, never a child's. A child package writes its own `M` records,
    and a consumer that compiles one module from two packages declares that module twice."""
    return [(target, m) for m in package.exported_modules_with_base(target)]


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
        for _, dirs, names in os.walk(abs_include):
            dirs[:] = [d for d in dirs if d not in _VCS_DIRS]  # a git object store holds no header
            stems |= {os.path.splitext(n)[0].lower() for n in names if n.endswith(suffixes)}
    return stems


def _is_unpacked_archive(target:BuildTarget, path:str) -> bool:
    """True when `path` is the include tree an artifactory archive unpacked into the build dir. That
    tree already carries its deployed layout, so a re-deploy copies it as it stands."""
    dep = target.dep
    return bool(dep.from_artifactory) and str(path).startswith(str(dep.build_dir))


def _include_deploy(target:BuildTarget, includes_root:str, abs_include:str):
    """(source dir, deployed dir, papa record) for one exported include dir. The record is the include
    path a consumer gets, so `as_includes_root` deploys src/mylib as include/mylib and records include."""
    root_path, root_src, alias = target.includes_root
    if root_path and abs_include == root_path:
        # An unpacked archive is already rooted. Rooting it again nests it one level per republish.
        if _is_unpacked_archive(target, root_src):
            return root_src, includes_root, 'I include'
        return f'{abs_include}/{os.path.basename(root_src)}', f'{includes_root}/{alias}', 'I include'
    name = os.path.basename(abs_include)
    if name == 'include': return abs_include, includes_root, 'I include'
    return abs_include, f'{includes_root}/{name}', f'I include/{name}'


def _same_file(a:str, b:str) -> bool:
    """True when both paths name one file. A case-variant spelling, or a deploy dir reached through a
    symlink, compares unequal as a string, and the strip would then edit the build artifact itself."""
    if package.match_path(a) == package.match_path(b): return True
    try: return os.path.samefile(a, b)
    except OSError: return False  # one of them does not exist yet, so they are two files


def _module_paths(modules) -> set:
    """The exact path of every gathered module, in the one spelling every path compare uses. The copy
    predicate reads the whole path, so a private module whose name ends the same way stays out."""
    return {package.match_path(m) for _, m in modules}


def _append_includes(target:BuildTarget, package_full_path, detail_echo, descr, includes, modules=()) -> int:
    """Deploy every exported include dir. Returns how many header files they hold, because one record
    names a whole dir and the record count alone never says how much a package ships."""
    if not includes:
        return 0 # nothing to do
    config = target.config
    includes_root = package_full_path + '/include' # output root
    # TODO: should we include .cpp files for easier debugging?
    # A module ships inside the include tree, so the copy carries it whatever order the hook used.
    module_sfx = tuple(package.match_path(s) for s in package.module_suffixes(m for _, m in modules))
    # A target that exports a module answers for every module file of ITS OWN tree, and of no other.
    # The exported include dir nearest a file names that tree, whatever tree physically holds it.
    roots = sorted({package.match_path(i) for _, i in includes}, key=len, reverse=True)
    bases = {package.match_path(b) for b in (package.module_base_dir(t, m) for t, m in modules) if b}
    module_paths = _module_paths(modules)

    def exports_modules(fwd:str) -> bool:
        """True when the exported include dir nearest to `fwd` exported a module of its own."""
        return next((r in bases for r in roots if fwd.startswith(r + '/')), False)

    suffixes = tuple(package.match_path(s) for s in target.include_glob_filter)
    stems = _header_stems(includes, suffixes + module_sfx)
    shipped = 0  # copy_dir runs this filter once per file, so the count costs no extra walk

    def is_header(path:str) -> bool:
        nonlocal shipped
        # the recipe and the filesystem can spell one name two ways, so the suffix reads the same rule
        name = package.match_path(os.path.basename(path))
        # a module source ships only when export_modules named it, so a private one beside it stays out
        fwd = package.match_path(path)
        # the compiler reads Api.IXX as a module, so the extension test folds case on every filesystem
        if name.lower().endswith(package.MODULE_EXTENSIONS) and exports_modules(fwd):
            header = fwd in module_paths
        else:
            # Qt-style stub headers carry no extension (`#include <QCoro/QCoroTask>`). Ship one only when
            # the header it forwards to is in the tree, so a LICENSE or an AUTHORS file never ships.
            header = name.endswith(suffixes) or ('.' not in name and name.lower() in stems)
        if header: shipped += 1
        return header

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
    return shipped


def _append_modules(target:BuildTarget, package_full_path, detail_echo, descr, modules) -> int:
    """Record every exported module. It copies nothing, because the include deploy already shipped the
    file. Two copies of one module would double the archive and give a consumer an ambiguous source."""
    includes_root = package_full_path + '/include'
    shipped = 0
    for modtarget, module in modules:
        base = package.module_base_dir(modtarget, module)
        # the copy maps src_dir onto dst_dir, so the module follows the same pair. An includes root
        # ships one subdir of the export, so its src_dir is deeper than the exported include.
        src_dir, dst_dir, _ = _include_deploy(modtarget, includes_root, base)
        fwd = forward_slashes(module)  # one backslash here would drop the module with no error
        if not package.match_path(module).startswith(package.match_path(src_dir) + '/'):
            warning(f'export_modules skipped {module}: the include deploy did not carry it.')
            continue
        deployed = dst_dir + fwd[len(src_dir):]
        if not os.path.exists(deployed):
            warning(f'export_modules skipped {module}: the include filter did not ship it.')
            continue
        record = f'M {os.path.relpath(deployed, package_full_path)}'.replace('\\', '/')
        descr.append(record)
        shipped += 1
        if detail_echo: console(f'    M ({modtarget.name+")": <16}  {record[2:]}')
    return shipped


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
    # so a load can spot a gcc tree in a clang build. `O` records every other axis of the objects,
    # so a consumer can spot a debug or a sanitized package in a plain release build.
    descr = [ f'P {target.name}' ]
    compiler = _compiler_stamp(config)
    if compiler: descr.append(f'C {compiler}')
    descr.append(f'O {build_names.object_attributes(target)}')
    for d in dependencies:
        if detail_echo: console(f'    D {d.dep_source}')
        descr.append(f'D {d.dep_source.get_papa_string()}')
        # A `D` record ends in a variable-length arg list, so the suffix cannot ride along. Its own
        # record keeps every older reader working, because an unknown record parses as nothing.
        suffix = d.dep_source.version_suffix
        if suffix: descr.append(f'V {d.dep_source.name} {suffix}')

    # An in-place deploy makes the package and the build tree one thing. The strip would take the
    # module objects the producer's own binaries link, so refuse before anything here removes a file.
    if _same_file(package_full_path, target.build_dir()) and \
       any(package.strips_module_objects(target, l) for l in target.exported_libs):
        raise RuntimeError(f'papa_deploy refused: {package_full_path} is the build dir itself, so the module ' + \
                           'objects cannot be dropped from the package alone. Deploy to a separate dir, ' + \
                           'or pass strip_objects=False to export_modules().')

    # Delete the include tree the last deploy wrote. A header this target no longer exports must not
    # ship. The copy below keeps every mtime, so a consumer still sees no change in an unchanged header.
    remove_tree(f'{package_full_path}/include')
    # the modules come first, because the include copy needs their suffixes to carry them
    modules = _gather_modules(target)
    includes = _gather_includes(target, r_includes)
    # a recursive bundle copies the child include trees, so their modules must ride along with them
    copied = package.consumed_modules(target) if r_includes else modules
    headers = _append_includes(target, package_full_path, detail_echo, descr, includes, copied)
    _warn_about_duplicate_include_trees(target, package_full_path)
    shipped_modules = _append_modules(target, package_full_path, detail_echo, descr, modules)

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
        if not _same_file(lib, outpath):
            if config.verbose: console(f'    copy {lib}\n      -> {outpath}')
            copy_if_needed(lib, outpath)
            # Only the packaged copy loses its module objects. The build dir keeps a linkable archive.
            package.strip_module_objects(libtarget, outpath)
        elif package.strips_module_objects(libtarget, outpath):
            # An in-place deploy makes the package and the build artifact one file. The strip would
            # take the objects the producer's own binaries link, so refuse instead of publishing both.
            raise RuntimeError(f'papa_deploy refused: {outpath} is the build artifact itself, so the module ' + \
                               'objects cannot be dropped from the package alone. Deploy to a separate dir, ' + \
                               'or pass strip_objects=False to export_modules().')

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

    config.deploy_stats.record(package_full_path, (len(includes), len(libs), len(syslibs), len(assets)))
    if config.print:
        mods = f', {shipped_modules} modules' if shipped_modules else ''
        console(f'  PAPA Deployed: {len(includes)} includes ({headers} files){mods}, {len(libs)} libs, ' + \
                f'{len(syslibs)} syslibs, {len(assets)} assets')


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
        self.attributes = [] # 'debug'/'release', platform, arch, variant tokens. [] predates the O record
        self.dependencies = []
        self.includes = []
        self.libs = []
        self.syslibs = []
        self.modules = [] # C++20 module sources. [] predates the M record
        self.assets: List[Asset] = []

        suffixes = {}  # dep name -> version_suffix, applied below once every `D` record is in

        def append_to(to:list, line):
            to.append(normalized_join(self.papa_dir, line[2:].strip()))

        for line in read_lines_from(self.papa_file):
            if   line.startswith('P '): self.project_name = line[2:].strip()
            elif line.startswith('C '): self.compiler = line[2:].strip()
            elif line.startswith('O '): self.attributes = line[2:].split()
            elif line.startswith('D '): self.dependencies.append(make_dep_source(line[2:].strip()))
            elif line.startswith('V '):
                dep_name, _, suffix = line[2:].strip().partition(' ')
                suffixes[dep_name] = suffix.strip()
            elif line.startswith('I '): append_to(self.includes, line)
            elif line.startswith('L '): append_to(self.libs, line)
            elif line.startswith('S '): append_to(self.syslibs, line)
            elif line.startswith('M '): append_to(self.modules, line)
            elif line.startswith('A '):
                relpath = line[2:].strip()
                fullpath = normalized_join(self.papa_dir, relpath)
                self.assets.append(Asset(relpath, fullpath, None))

        # applied after the loop, because a `V` record may sit either side of the `D` record it names
        for dep_source in self.dependencies:
            if dep_source.name in suffixes: dep_source.version_suffix = suffixes[dep_source.name]
