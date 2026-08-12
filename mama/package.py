from __future__ import annotations
from typing import List, TYPE_CHECKING
import os
from .utils.system import console, System, warning
from .utils.paths import (normalized_path, normalized_join, forward_slashes,
                          glob_with_name_match, glob_with_extensions)
from .utils.errors import BuildError
from .utils.sub_process import execute_piped, SubProcess
from .types.asset import Asset

if TYPE_CHECKING:
    from .build_target import BuildTarget
    from .build_config import BuildConfig

# Every spelling of a C++20 module interface unit. MSVC writes `.ixx`, the others write `.cppm`.
MODULE_EXTENSIONS = ('.cppm', '.ixx', '.ccm', '.cxxm', '.c++m', '.mpp')


def is_a_static_library(lib: str):
    if not lib: return False
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


def export_modules(target: BuildTarget, module_path: str, modules, build_dir: bool):
    module_path = target_root_path(target, module_path, build_dir=build_dir)
    if modules is None:
        found = sorted(glob_with_extensions(module_path, list(MODULE_EXTENSIONS)))
    else:
        found = [normalized_join(module_path, m) for m in modules]
    added = False
    for module in found:
        if not os.path.exists(module):
            warning(f'export_modules failed to find: {module}')
            continue
        if not module in target.exported_modules:
            target.exported_modules.append(module)
            added = True
    return added


def module_suffixes(modules) -> tuple:
    """The distinct extensions of `modules`, so the include deploy carries them too.
    It reads the gathered list, not one target, because a recursive deploy ships a child's modules."""
    return tuple({os.path.splitext(m)[1] for m in modules})


def module_base_dir(target: BuildTarget, module: str) -> str:
    """The exported include dir that holds `module`, longest match first, or '' when none does.
    Cmake needs every module of a file set to sit under one of its base dirs. The compare reads both
    paths as forward slashes, because one backslash would drop the module with no error."""
    fwd = forward_slashes(module)
    best = ''
    for include in target.exported_includes:
        if fwd.startswith(forward_slashes(include) + '/') and len(include) > len(best):
            best = include  # the caller matches this against the export list, so keep its spelling
    return best


def drop_nested_dirs(dirs) -> list:
    """The given dirs, sorted, with every dir that sits inside another one removed. Cmake refuses a
    file set whose base dirs contain each other, and the outer dir already holds them all."""
    uniq = sorted({forward_slashes(d) for d in dirs if d})
    return [d for d in uniq if not any(d.startswith(outer + '/') for outer in uniq if outer != d)]


def module_base_dirs(target: BuildTarget) -> list:
    """The exported include dirs that hold a module of this target, with every nested dir dropped."""
    return drop_nested_dirs(module_base_dir(target, m) for m in target.exported_modules)


def exported_modules_with_base(target: BuildTarget) -> list:
    """The exported modules that an exported include dir holds. A module outside every include path
    reaches no consumer, and cmake refuses a file set whose FILES sit under no base dir."""
    return [m for m in target.exported_modules if module_base_dir(target, m)]


def _module_object_members(target: BuildTarget, lib: str) -> list:
    """The archive members that hold a module initializer, read from the archive itself.
    Reading the listing covers every object suffix, so no platform has to declare one.
    A build system names the object after the source, with or without the module extension. The
    exact name wins, so `foo.o` built from `foo.cpp` never answers for a `foo.cppm` beside it."""
    listing = execute_piped(target.config.platform.list_archive_members_cmd(lib), throw=False)
    if not listing: return []
    # one member per line, so a module file name that holds a space survives the parse
    objects = [m for m in (ln.strip() for ln in listing.splitlines())
               if os.path.splitext(m)[1] in ('.o', '.obj')]
    found = []
    for module in target.exported_modules:
        base = os.path.basename(module)
        hits = [o for o in objects if os.path.splitext(o)[0] == base]
        if not hits: hits = [o for o in objects if os.path.splitext(o)[0] == os.path.splitext(base)[0]]
        found += hits
    return sorted(set(found))


def strip_module_objects(target: BuildTarget, lib: str):
    """Removes the module objects from a packaged static library.

    A module interface unit emits a strong `initializer for module X` symbol. The consumer compiles
    the same source, so a whole-archive link finds two definitions and fails. The consumer always
    supplies that symbol, so the package does not need it."""
    if not target.strip_module_objects or not target.exported_modules: return
    if not is_a_static_library(lib): return
    members = _module_object_members(target, lib)
    if not members: return
    # the arg list stays a list, because SubProcess splits a joined string on every space
    status = SubProcess.run(target.config.platform.remove_from_archive_cmd(lib, members))
    if status != 0: raise BuildError(f'Failed to remove {len(members)} module objects from {lib}')
    if target.config.print:
        warning(f'  Removed {len(members)} module objects from {os.path.basename(lib)}')


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
