from __future__ import annotations
from typing import List, TYPE_CHECKING
import os
from .utils.system import console, System, warning
from .utils.paths import (normalized_path, normalized_join, forward_slashes,
                          glob_with_name_match, glob_with_extensions)
from .utils.errors import BuildError
from .utils.fileio import copy_if_needed
from .utils.sub_process import execute_piped, execute_piped_echo, SubProcess
from .types.asset import Asset

if TYPE_CHECKING:
    from .build_target import BuildTarget
    from .build_config import BuildConfig

# Every spelling of a C++20 module interface unit. MSVC writes `.ixx`, the others write `.cppm`.
MODULE_EXTENSIONS = ('.cppm', '.ixx', '.ccm', '.cxxm', '.c++m', '.mpp')

# The dir that holds the exported archive after the strip, beside the archive the build wrote.
MODULE_STRIP_DIR = 'mama-nomodules'

# Every non-module C++ source. An object named after one of these is not a module object.
SOURCE_EXTENSIONS = ('.cpp', '.cc', '.cxx', '.c++', '.c')


def is_a_static_library(lib: str):
    if not lib: return False
    lib = match_path(lib)  # a filesystem that ignores case reads Producer.LIB as the same archive
    return lib.endswith('.a') or lib.endswith('.lib')


def is_a_dynamic_library(lib: str):
    if not lib: return False
    return lib.endswith('.dll')    or lib.endswith('.pdb') \
        or lib.endswith('.dylib')  or lib.endswith('.so')  \
        or lib.endswith('.bundle') or lib.endswith('.framework') \
        or lib.endswith('.aar')    or (str.isdigit(lib[-1]) and '.so.' in lib) # allow versioned .so.1.2.3 files


def is_a_library(lib: str):
    return is_a_static_library(lib) or is_a_dynamic_library(lib)


def target_root_path(target: BuildTarget, path: str, build_dir: bool):
    root = target.build_dir() if build_dir else target.source_dir()
    return normalized_join(root, path)


def get_lib_basename(lib: str|tuple):
    """The name that identifies a lib, for dedup and for the papa manifest. A tuple is
    (path, alias). An Apple framework has no file of its own, so the string itself is the name."""
    if isinstance(lib, tuple): return os.path.basename(lib[0])
    if lib.startswith('-framework '): return lib
    return os.path.basename(lib)


def get_unique_libnames(items: list):
    added = set()
    unique = list() # a list, to preserve the item order
    for item in items:
        basename = get_lib_basename(item)
        if not basename in added:
            added.add(basename)
            unique.append(item)
    return unique


def _overlapping_include(exported: List[str], include_path: str) -> str:
    """The already exported include that shares a tree with `include_path`, or None. QCoro exports both
    `include` and `include/qcoro`, and the second export names headers the first one already covers."""
    nested = include_path + '/'
    for other in exported:
        if nested.startswith(other + '/') or (other + '/').startswith(nested): return other
    return None


def export_include(target: BuildTarget, include_path: str, build_dir: bool,
                   as_includes_root:bool|str=False):
    include_path = target_root_path(target, include_path, build_dir=build_dir)
    if os.path.exists(include_path):
        if as_includes_root:
            # export the parent of this include, so a consumer writes #include <mylib/file.h>, not <src/mylib/file.h>
            includes_root = include_path
            if type(as_includes_root) == str:
                alias_name = str(as_includes_root) # the user passed the alias, e.g. 'mylib'
            else:
                alias_name = os.path.basename(include_path) # take '{src}/include/mylib' -> 'mylib'
            include_path = normalized_path(include_path + '/../')
            target.includes_root = (include_path, includes_root, alias_name)
        if not include_path in target.exported_includes:
            # an as_includes_root export records the parent but ships one subdir, so compare against that subdir
            root_path, root_src, _ = target.includes_root
            shipped = [root_src if e == root_path else e for e in target.exported_includes]
            overlap = _overlapping_include(shipped, include_path)
            if overlap and overlap != include_path:  # naming that same dir again only adds its include path
                warning(f'export_include({include_path}) overlaps the exported {overlap}. ' + \
                        'One export covers those headers already.')
            target.exported_includes.append(include_path)
        return True
    # A named include path that is not on disk is a packaging fault, like a missing lib. The fallback
    # default_package_includes() can pick a shallower dir and ship headers no consumer can reach, so report it.
    warning(f'export_include failed to find: {include_path}')
    return False


def export_includes(target: BuildTarget, include_paths, build_dir: bool):
    added = False
    if isinstance(include_paths, str):
        return target.export_include(include_paths, build_dir)
    elif isinstance(include_paths, list):
        for include_path in include_paths:
            added |= target.export_include(include_path, build_dir)
    return added


def export_modules(target: BuildTarget, module_path: str, modules, build_dir: bool, recursive=True):
    module_path = target_root_path(target, module_path, build_dir=build_dir)
    if isinstance(modules, str): modules = [modules]  # one name, not a sequence of characters
    if modules is None:
        found = sorted(glob_with_extensions(module_path, list(MODULE_EXTENSIONS), recursive=recursive))
    else:
        found = [normalized_join(module_path, m) for m in modules]
    added = False
    seen = {match_path(m) for m in target.exported_modules}  # one file, whatever case the caller spelled
    for module in found:
        if not os.path.exists(module):
            warning(f'export_modules failed to find: {module}')
            continue
        if match_path(module) not in seen:
            seen.add(match_path(module))
            target.exported_modules.append(module)
            added = True
    return added


def default_package_modules(target: BuildTarget) -> bool:
    """Export every module interface unit under the exported include dirs. A library that ships one
    almost always publishes it, and an explicit export_modules() call narrows this down."""
    added = False
    for include in target.exported_includes:
        added |= export_modules(target, include, None, build_dir=False)
    return added


def module_suffixes(modules) -> tuple:
    """The distinct extensions of `modules`, so the include deploy carries them too.
    It reads the gathered list, not one target, because the caller decides which modules ship."""
    return tuple({os.path.splitext(m)[1] for m in modules})


def match_path(path: str) -> str:
    """The spelling that compares two paths: forward slashes, and one case where the filesystem
    ignores case. A raw compare drops a module whose export named the same dir another way."""
    fwd = forward_slashes(path)
    return fwd.lower() if System.windows or System.macos else fwd


def _holds_path(directory: str, path: str) -> bool:
    """True when `directory` really is an ancestor of `path`. A case-sensitive volume holds two dirs
    whose folded names are equal, so only the filesystem can tell that pair apart."""
    ancestor = os.path.dirname(path)
    while len(ancestor) >= len(directory):
        try:
            if os.path.samefile(ancestor, directory): return True
        except OSError:
            return False  # one of them is gone, so they name no common dir
        parent = os.path.dirname(ancestor)
        if parent == ancestor: return False
        ancestor = parent
    return False


def module_base_dir(target: BuildTarget, module: str) -> str:
    """The exported include dir that holds `module`, longest match first, or '' when none does.
    Cmake needs every module of a file set to sit under one of its base dirs."""
    fwd = match_path(module)
    raw = forward_slashes(module)
    # as_includes_root deploys one subdir of the export, so a module outside it never reaches a consumer
    root_src = match_path(target.includes_root[1]) if target.includes_root[1] else ''
    if root_src and not fwd.startswith(root_src + '/'): return ''
    best = ''
    for include in target.exported_includes:
        if not fwd.startswith(match_path(include) + '/') or len(include) <= len(best): continue
        # only a match that needed case folding asks the filesystem, so the exact path costs nothing
        if raw.startswith(forward_slashes(include) + '/') or _holds_path(include, module):
            best = include  # the caller matches this against the export list, so keep its spelling
    return best


def drop_nested_dirs(dirs) -> list:
    """The given dirs, sorted, with every dir that sits inside another one removed. Cmake refuses a
    file set whose base dirs contain each other, and the outer dir already holds them all."""
    uniq = sorted({forward_slashes(d) for d in dirs if d})
    return [d for d in uniq
            if not any(match_path(d).startswith(match_path(o) + '/') for o in uniq if o != d)]


def module_base_dirs(target: BuildTarget) -> list:
    """The exported include dirs that hold a module of this target, with every nested dir dropped."""
    return drop_nested_dirs(module_base_dir(target, m) for m in target.exported_modules)


def exported_modules_with_base(target: BuildTarget) -> list:
    """The exported modules that an exported include dir holds. A module outside every include path
    reaches no consumer, and cmake refuses a file set whose FILES sit under no base dir."""
    return [m for m in target.exported_modules if module_base_dir(target, m)]


def warn_unreachable_modules(target: BuildTarget) -> None:
    """Warn once for a module no exported include dir holds. Every later step drops such a module
    without a word, so this is where the recipe author can still see the mistake."""
    for module in target.exported_modules:
        if not module_base_dir(target, module):
            warning(f'{target.name}: export_modules skipped {module}. No exported include dir holds it.')


def consumed_modules(target: BuildTarget) -> list:
    """Every module whose object this archive can hold: the ones this target exports, and the ones
    every package below it exports. `mama_target_modules` compiles the whole dependency tree into
    this target, so a grandchild module the strip misses defines its initializer twice."""
    modules = list(exported_modules_with_base(target))
    seen = set()
    def walk(parent: BuildTarget):
        for child in parent.children():
            if child.name in seen: continue  # a diamond reaches one package through two parents
            seen.add(child.name)
            modules.extend(exported_modules_with_base(child.target))
            walk(child.target)
    walk(target)
    return modules


def _archive_members(target: BuildTarget, lib: str) -> list:
    """The object members of `lib`, one per line, through the archiver of this platform.
    A failed listing raises, because treating it as an empty archive publishes the module objects."""
    cmd = target.config.platform.list_archive_members_cmd(lib)
    status, listing = execute_piped_echo(None, cmd, echo=False)
    if status != 0:
        raise BuildError(f'Failed to list {lib} with {cmd[0]}. The module objects cannot be removed.')
    # one member per line, so a module file name that holds a space survives the parse
    return [m for m in (ln.strip() for ln in listing.splitlines())
            if os.path.splitext(m)[1].lower() in ('.o', '.obj')]


def _shared_tail(a: list, b: list) -> int:
    """The number of trailing path components two split paths share."""
    n = 0
    while n < len(a) and n < len(b) and a[-1 - n] == b[-1 - n]: n += 1
    return n


def _ambiguous_names(target: BuildTarget) -> set:
    """Every name a member could also have come from: a non-module source without its extension, and
    a module this target does not export with its extension. Either one makes a bare member unsafe."""
    exported = {match_path(m) for m in target.exported_modules}
    names = set()
    for root, _, files in os.walk(target.source_dir()):
        for name in files:
            stem, ext = os.path.splitext(name)
            ext = ext.lower()
            if ext in SOURCE_EXTENSIONS: names.add(match_path(stem))
            elif ext in MODULE_EXTENSIONS and match_path(normalized_join(root, name)) not in exported:
                names.add(match_path(name))
    return names


def _module_object_members(target: BuildTarget, lib: str) -> list:
    """The archive members that hold a module initializer, read from the archive itself.

    Each module takes the members that share the most trailing path components with it. That answer
    keeps an exported `pub/api.cppm` away from the private `api.cppm` beside it. MSVC drops the module
    extension from the object name, so a module that shares no component falls back to its bare name.
    A non-module source of that name in this target makes the name ambiguous, and the member stays.
    An archiver that stores no path lists a private unit under the name of an exported one, so a name
    the exported modules cannot account for keeps every copy."""
    objects = _archive_members(target, lib)
    # an archiver lists the path it stored, and the compare walks that path from its end
    parts = [(o, match_path(os.path.splitext(forward_slashes(o))[0]).split('/')) for o in objects]
    claims, names = {}, None
    for module in consumed_modules(target):
        module_parts = match_path(module).split('/')
        scored = [(o, _shared_tail(p, module_parts)) for o, p in parts]
        best = max((n for _, n in scored), default=0)
        if names is None: names = _ambiguous_names(target)  # one walk, whatever the scores need
        if best == 1 and module_parts[-1] in names:
            warning(f'{target.name}: a module named {module_parts[-1]} is not exported, and the '
                    'archive names no path, so the module objects of that name stay.')
            continue
        if not best:
            bare = match_path(os.path.splitext(module_parts[-1])[0])
            if bare in names: continue
            scored, best = [(o, 1) for o, p in parts if p[-1] == bare], 1
        for name in {o for o, n in scored if n and n == best}:
            claims[name] = claims.get(name, 0) + 1

    members = []
    for name, claimed in claims.items():
        held = objects.count(name)
        if held > claimed:
            warning(f'{target.name}: {os.path.basename(lib)} holds {held} members named {name}, and '
                    f'{claimed} exported module(s) claim it. Keeping them, so no definition is lost.')
            continue
        members += [name] * held  # `ar d` drops one member per name it is given
    return members


def strips_module_objects(target: BuildTarget, lib: str) -> bool:
    """True when this lib can lose module objects on the way into a package. The archive decides the
    rest: a target that compiled no module interface into it keeps the archive the build wrote."""
    return bool(target.strip_module_objects and is_a_static_library(lib)
                and not is_a_thin_archive(lib) and consumed_modules(target))


def is_a_thin_archive(lib: str) -> bool:
    """True for a GNU thin archive, which names each member by a path instead of holding it.
    A copy of one resolves every path against its new dir, so the members are gone."""
    try:
        with open(lib, 'rb') as f: return f.read(8) == b'!<thin>\n'
    except OSError:
        return False


def _unstripped_lib(lib: str) -> str:
    """The archive a stripped copy came from, and `lib` itself for any other path.
    A later run reads back the recorded export, which already names the copy."""
    head, tail = os.path.split(os.path.dirname(lib))
    return normalized_join(head, os.path.basename(lib)) if tail == MODULE_STRIP_DIR else lib


def export_stripped_module_libs(target: BuildTarget):
    """Point every exported static library at a copy that holds no module object.

    A consumer that builds this target from source links `exported_libs` directly, and the build dir
    archive must keep those objects for the binaries of this target. The copy keeps the file name, so
    only the directory differs. An archive that holds no module object keeps its own path, and a
    fetched package is already stripped, so both copy nothing."""
    if target.dep.from_artifactory or not target.strip_module_objects: return
    if not consumed_modules(target): return
    for i, lib in enumerate(target.exported_libs):
        if not isinstance(lib, str) or not is_a_static_library(lib): continue
        # read the archive this build wrote, never the copy an earlier run recorded as the export
        src = _unstripped_lib(lib)
        if not os.path.exists(src): continue
        if is_a_thin_archive(src):
            warning(f'  {os.path.basename(src)} is a thin archive, so its module objects stay. ' + \
                    'A thin archive names each member by a path, and a copy breaks every one.')
            continue
        members = _module_object_members(target, src)
        if not members:
            target.exported_libs[i] = src  # this build wrote no module object, so publish it as it is
            continue
        out = normalized_join(os.path.dirname(src), MODULE_STRIP_DIR, os.path.basename(src))
        os.makedirs(os.path.dirname(out), exist_ok=True)
        copy_if_needed(src, out)
        _remove_members(target, out, members)  # the copy holds what the listing of `src` named
        target.exported_libs[i] = out


def _remove_members(target: BuildTarget, lib: str, members: list):
    """Removes the named object members from a static library."""
    # the arg list stays a list, because SubProcess splits a joined string on every space
    status = SubProcess.run(target.config.platform.remove_from_archive_cmd(lib, members))
    if status != 0: raise BuildError(f'Failed to remove {len(members)} module objects from {lib}')
    # Always warn: this removes whole objects, so a module unit that defines a non-inline function
    # loses that definition for a consumer whose toolchain builds no module.
    warning(f'  Removed {len(members)} module objects from {os.path.basename(lib)}. ' + \
            'An exported module must define nothing but its own interface.')


def strip_module_objects(target: BuildTarget, lib: str):
    """Removes the module objects from a packaged static library.

    A module interface unit emits a strong `initializer for module X` symbol. The consumer compiles
    the same source, so a whole-archive link finds two definitions and fails. The consumer always
    supplies that symbol, so the package does not need it."""
    if not strips_module_objects(target, lib): return
    members = _module_object_members(target, lib)
    if members: _remove_members(target, lib, members)


def export_lib(target: BuildTarget, relative_path: str, build_dir: bool):
    path = target_root_path(target, relative_path, build_dir=build_dir)
    if os.path.exists(path):
        target.exported_libs.append(path)
        target.exported_libs = get_unique_libnames(target.exported_libs)
    else:
        console(f'export_lib failed to find: {path}')


def set_export_libs_and_products(target: BuildTarget, libs_and_deps: List[str]):
    """Sets the target's exported_libs and build_products from a serialized list of libs and deps."""
    libs_and_deps = cleanup_libs_list(libs_and_deps)
    only_libs = []
    for lib in libs_and_deps:
        if os.path.exists(lib) and is_a_library(lib):
            only_libs.append(lib)
    target.exported_libs = get_unique_libnames(only_libs)
    target.build_products = get_unique_libnames(libs_and_deps)


def cleanup_libs_list(libs: List[str]):
    """Strips whitespace and drops `.lib.recipe` entries."""
    cleaned = []
    for lib in libs:
        lib = lib.strip()
        if not lib.endswith('.lib.recipe'):
            cleaned.append(lib)
    return cleaned


def clean_intermediate_files(target: BuildTarget):
    if target.dep.always_build or target.dep.is_root:
        return

    config: BuildConfig = target.config
    should_clean = False

    if target.clean_intermediate_files:
        if config.verbose: warning('  clean_intermediate [target.clean_intermediate_files]')
        should_clean = True
    elif config.upload:
        if config.verbose: warning('  clean_intermediate [config.upload]')
        should_clean = True
    # no targeted build: this was an automatic dependency build from source
    elif (config.build or config.rebuild or config.update) and config.no_specific_target():
        if config.verbose: warning('  clean_intermediate [dependency build cleanup]')
        should_clean = True

    if not should_clean:
        return

    files_to_clean = glob_with_extensions(target.build_dir(), ['.obj', '.o'])
    if files_to_clean:
        if target.config.print:
            warning(f'Cleaning {len(files_to_clean)} intermediate files in {target.build_dir()}')
        for file in files_to_clean:
            if os.path.isfile(file):
                os.remove(file)


def export_libs(target: BuildTarget, path, pattern_substrings: List[str], build_dir: bool, order: list):
    root_path = target_root_path(target, path, build_dir=build_dir)
    libs = glob_with_name_match(root_path, pattern_substrings)
    libs = cleanup_libs_list(libs)

    root_deploy = root_path + '/deploy/'
    libs = [l for l in libs if not l.startswith(root_deploy)]

    target.exported_libs += libs
    target.exported_libs = get_unique_libnames(target.exported_libs)

    # apply the order to ALL exported libs, because earlier export steps added libs too
    if order:
        def lib_index(lib):
            for i in range(len(order)):
                if order[i] in lib: return i
            return len(order)  # an unmatched lib sorts last
        def sort_key(lib):
            return lib_index(lib)
        target.exported_libs.sort(key=sort_key)

    return len(target.exported_libs) > 0


def export_asset(target: BuildTarget, asset: str, category=None, build_dir=False):
    full_asset = target_root_path(target, asset, build_dir=build_dir)
    if os.path.exists(full_asset):
        target.exported_assets.append(Asset(asset, full_asset, category))
        return True
    else:
        console(f'export_asset failed to find: {full_asset}')
        return False


def export_assets(target: BuildTarget, assets_path: str, pattern_substrings: list, category=None, build_dir=True):
    assets_path += '/'
    assets = glob_with_name_match(target_root_path(target, assets_path, build_dir=build_dir),
                                  pattern_substrings, match_dirs=False)
    if assets:
        for full_asset in assets:
            target.exported_assets.append(Asset(assets_path, full_asset, category))
        return True
    return False


def find_syslib(target: BuildTarget, name: str, apt: bool, required: bool):
    platform = target.config.platform
    if platform.syslib_is_framework:
        if not name.startswith('-framework '):
            raise EnvironmentError(f'Expected "-framework name" but got "{name}"')
        return name # '-framework Foundation'
    elif platform.syslib_is_searchable:
        roots = [ "/usr/lib" ]

        if 'LD_LIBRARY_PATH' in os.environ:
            roots += os.environ['LD_LIBRARY_PATH'].split(':')

        compiler_dir = 'aarch64-linux-gnu' if System.aarch64 else 'x86_64-linux-gnu'
        for root in roots:
            for candidate in [
                lambda: f'{root}/{compiler_dir}/{name}',
                lambda: f'{root}/{compiler_dir}/lib{name}.so',
                lambda: f'{root}/{compiler_dir}/lib{name}.so.2',
                lambda: f'{root}/{compiler_dir}/lib{name}.a',
                lambda: f'{root}/lib{name}.so',
                lambda: f'{root}/lib{name}.so.2',
                lambda: f'{root}/lib{name}.a' ]:
                if os.path.exists(candidate()):
                    return name # found e.g. `libdl.so`, so return `dl` for the linker
        if not required: return None
        if apt: raise IOError(f'Error {target.name} failed to find REQUIRED SysLib: {name}  Try `sudo apt install {apt}`')
        raise IOError(f'Error {target.name} failed to find REQUIRED SysLib: {name}  Try installing it with apt.')
    else:
        return name # export as-is and expect the system linker to find it


def export_syslib(target: BuildTarget, name: str, apt: bool, required: bool):
    """
    - target: The build target that exports the syslib
    - name: Name of the system library, eg: lzma
    - apt: Name of the apt package to suggest when the search fails
    - required: If true, a missing syslib raises an exception
    """
    try:
        lib = find_syslib(target, name, apt, required)
        if lib:
            target.exported_syslibs.append(lib)
            target.exported_syslibs = get_unique_libnames(target.exported_syslibs)
            return True
    except IOError:
        if target.config.clean:
            # export as-is and expect the system linker to find it
            target.exported_syslibs.append(name)
            target.exported_syslibs = get_unique_libnames(target.exported_syslibs)
            return True
        else:
            raise
    warning(f'WARNING: SysLib {name} not found for target {target.name}, ignoring.')
    return False


def _reset_syslib_name(syslib: str):
    """Resets a syslib name from `/usr/lib/x86_64-linux-gnu/liblzma.so` to `lzma`."""
    fname = os.path.basename(syslib)
    if fname.startswith('lib'):
        if fname.endswith('.so'):
            return fname[3:-3]  # pop 'lib'(3) from front and '.so'(3) from back
        if fname.endswith('.a'):
            return fname[3:-2]  # pop 'lib' and '.a'
    return fname


def reload_syslibs(target: BuildTarget, syslibs: List[str]):
    reloaded = []
    for syslib in syslibs:
        if syslib.startswith('-framework '):
            reloaded.append(syslib)
        else:
            libname = _reset_syslib_name(syslib)
            lib = find_syslib(target, libname, apt=None, required=False)
            if not lib: lib = syslib # not found, keep the original syslib
            reloaded.append(lib)
    target.exported_syslibs = reloaded
