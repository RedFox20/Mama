from __future__ import annotations
from typing import List, TYPE_CHECKING
import os
from .utils.system import console, System
from .util import normalized_path, glob_with_name_match, glob_with_extensions
from .types.asset import Asset

if TYPE_CHECKING:
    from .build_target import BuildTarget
    from .build_config import BuildConfig

def is_a_static_library(lib: str):
    return lib.endswith('.a') or lib.endswith('.lib')


def is_a_dynamic_library(lib: str):
    return lib.endswith('.dll')    or lib.endswith('.pdb') \
        or lib.endswith('.dylib')  or lib.endswith('.so')  \
        or lib.endswith('.bundle') or lib.endswith('.framework') \
        or lib.endswith('.aar')


def is_a_library(lib: str):
    return is_a_static_library(lib) or is_a_dynamic_library(lib)


def target_root_path(target: BuildTarget, path: str, build_dir: bool):
    root = target.build_dir() if build_dir else target.source_dir()
    return normalized_path(os.path.join(root, path))


def get_lib_basename(lib: str|tuple):
    if isinstance(lib, tuple):
        return os.path.basename(lib[0])
    elif lib.startswith('-framework '):
        return lib.split(' ', 1)[1]
    else:
        return os.path.basename(lib)


def get_unique_libnames(items: list):
    unique = dict()
    for item in items:
        basename = get_lib_basename(item)
        unique[basename] = item
    return list(unique.values())


def export_include(target: BuildTarget, include_path: str, build_dir: bool):
    include_path = target_root_path(target, include_path, build_dir=build_dir)
    if os.path.exists(include_path):
        #console(f'export_include={include_path}')
        if not include_path in target.exported_includes:
            target.exported_includes.append(include_path)
        return True
    return False


def export_includes(target: BuildTarget, include_paths, build_dir: bool):
    added = False
    if isinstance(include_paths, str):
        return target.export_include(include_paths, build_dir)
    elif isinstance(include_paths, list):
        for include_path in include_paths:
            added |= target.export_include(include_path, build_dir)
    return added


def export_lib(target: BuildTarget, relative_path: str, build_dir: bool):
    path = target_root_path(target, relative_path, build_dir=build_dir)
    if os.path.exists(path):
        target.exported_libs.append(path)
        target.exported_libs = get_unique_libnames(target.exported_libs)
    else:
        console(f'export_lib failed to find: {path}')


def set_export_libs_and_products(target: BuildTarget, libs_and_deps: List[str]):
    """
    Sets target's exported_libs and build_products from previously serialized
    list of libraries and dependencies
    """
    libs_and_deps = cleanup_libs_list(libs_and_deps)
    only_libs = []
    for lib in libs_and_deps:
        if os.path.exists(lib) and is_a_library(lib):
            only_libs.append(lib)
    target.exported_libs = get_unique_libnames(only_libs)
    target.build_products = get_unique_libnames(libs_and_deps)


def cleanup_libs_list(libs: List[str]):
    """Cleans up libs list by removing invalid entries"""
    cleaned = []
    for lib in libs:
        lib = lib.strip()
        if not lib.endswith('.lib.recipe'):
            cleaned.append(lib)
    return cleaned


# NOTE: clean_intermediate_files is a suggestion !
def clean_intermediate_files(target: BuildTarget):
    # never clean root or always_build targets
    if target.dep.always_build or target.dep.is_root:
        return

    config: BuildConfig = target.config
    should_clean = False

    if target.clean_intermediate_files:
        if config.verbose: console('  clean_intermediate [target.clean_intermediate_files]', color='yellow')
        should_clean = True
    # always clean the intermediate files if we just did an upload operation
    elif config.upload:
        if config.verbose: console('  clean_intermediate [config.upload]', color='yellow')
        should_clean = True
    # do automatic cleaning if we did not do a targeted build -- this was an automatic build from source
    elif (config.build or config.rebuild or config.update) and config.no_specific_target():
        if config.verbose: console('  clean_intermediate [dependency build cleanup]', color='yellow')
        should_clean = True

    if not should_clean:
        return

    files_to_clean = glob_with_extensions(target.build_dir(), ['.obj', '.o'])
    if files_to_clean:
        if target.config.print:
            console(f'Cleaning {len(files_to_clean)} intermediate files in {target.build_dir()}', color='yellow')
        for file in files_to_clean:
            if os.path.isfile(file):
                os.remove(file)


def export_libs(target: BuildTarget, path, pattern_substrings: List[str], build_dir: bool, order: list):
    root_path = target_root_path(target, path, build_dir=build_dir)
    libs = glob_with_name_match(root_path, pattern_substrings)
    libs = cleanup_libs_list(libs)

    # ignore root_path/deploy
    root_deploy = root_path + '/deploy/'
    libs = [l for l in libs if not l.startswith(root_deploy)]

    if order:
        def lib_index(lib):
            for i in range(len(order)):
                if order[i] in lib: return i
            return len(order)  # if this lib name does not match, put it at the end of the list
        def sort_key(lib):
            return lib_index(lib)
        libs.sort(key=sort_key)
    target.exported_libs += libs
    target.exported_libs = get_unique_libnames(target.exported_libs)
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
    assets = glob_with_name_match(target_root_path(target, assets_path, build_dir=build_dir), pattern_substrings, match_dirs=False)
    if assets:
        for full_asset in assets:
            target.exported_assets.append(Asset(assets_path, full_asset, category))
        return True
    return False


def find_syslib(target: BuildTarget, name: str, apt: bool, required: bool):
    if target.ios or target.macos:
        if not name.startswith('-framework '):
            raise EnvironmentError(f'Expected "-framework name" but got "{name}"')
        return name # '-framework Foundation'
    elif target.linux:
        compiler_dir = 'aarch64-linux-gnu' if System.aarch64 else 'x86_64-linux-gnu'
        for candidate in [
            lambda: f'/usr/lib/{compiler_dir}/{name}',
            lambda: f'/usr/lib/{compiler_dir}/lib{name}.so',
            lambda: f'/usr/lib/{compiler_dir}/lib{name}.so.2',
            lambda: f'/usr/lib/{compiler_dir}/lib{name}.a',
            lambda: f'/usr/lib/lib{name}.so',
            lambda: f'/usr/lib/lib{name}.so.2',
            lambda: f'/usr/lib/lib{name}.a' ]:
            if os.path.exists(candidate()):
                return name # example: we found `libdl.so`, so just return `dl` for the linker
        if not required: return None
        if apt: raise IOError(f'Error {target.name} failed to find REQUIRED SysLib: {name}  Try `sudo apt install {apt}`')
        raise IOError(f'Error {target.name} failed to find REQUIRED SysLib: {name}  Try installing it with apt.')
    else:
        return name # just export it. expect system linker to find it.


def export_syslib(target: BuildTarget, name: str, apt: bool, required: bool):
    """
    - target: The build target where to add the export syslib
    - name: Name of the system library, eg: lzma
    - apt: if true, then apt suggestion is given
    - required: if true, then an exception is thrown if syslib is not found
    """
    try:
        lib = find_syslib(target, name, apt, required)
        if lib:
            #console(f'Exporting syslib: {name}:{lib}')
            target.exported_syslibs.append(lib)
            target.exported_syslibs = get_unique_libnames(target.exported_syslibs)
            return True
    except IOError:
        if target.config.clean:
            # just export it. expect system linker to find it.
            target.exported_syslibs.append(name)
            target.exported_syslibs = get_unique_libnames(target.exported_syslibs)
            return True
        else:
            raise
    return False


def get_lib_basename(syslib: str):
    if syslib.startswith('-framework '):
        return syslib
    return os.path.basename(syslib)


def _reset_syslib_name(syslib: str):
    """ Resets the syslib name from `/usr/lib/x86_64-linux-gnu/liblzma.so` to `lzma` """
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
            if not lib: lib = syslib # not found, fall back to original syslib
            reloaded.append(lib)
    target.exported_syslibs = reloaded
