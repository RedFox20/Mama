import os
from mama.system import console
from mama.util import normalized_path, glob_with_name_match
from .types.asset import Asset


def target_root_path(target, path, src_dir):
    root = target.source_dir() if src_dir else target.build_dir()
    return normalized_path(os.path.join(root, path))


def get_lib_basename(lib):
    if isinstance(lib, tuple):
        return os.path.basename(lib[0])
    elif lib.startswith('-framework '):
        return lib.split(' ', 1)[1]
    else:
        return os.path.basename(lib)


def get_unique_basenames(items):
    unique = dict()
    for item in items:
        basename = get_lib_basename(item)
        unique[basename] = item
    return list(unique.values())


def export_include(target, include_path, build_dir):
    include_path = target_root_path(target, include_path, not build_dir)
    #console(f'export_include={include_path}')
    if os.path.exists(include_path):
        if not include_path in target.exported_includes:
            target.exported_includes.append(include_path)
        return True
    return False


def export_includes(target, include_paths, build_dir):
    added = False
    for include_path in include_paths:
        added |= target.export_include(include_path, build_dir)
    return added


def export_lib(target, relative_path, src_dir):
    path = target_root_path(target, relative_path, src_dir)
    if os.path.exists(path):
        target.exported_libs.append(path)
        target.exported_libs = get_unique_basenames(target.exported_libs)
    else:
        console(f'export_lib failed to find: {path}')


def cleanup_libs_list(libs):
    """Cleans up libs list by removing invalid entries"""
    cleaned = []
    for lib in libs:
        lib = lib.strip()
        if not lib.endswith('.lib.recipe'):
            cleaned.append(lib)
    return cleaned


def clean_intermediate_files(target):
    files_to_clean = glob_with_name_match(target.build_dir(), ['.obj', '.o'])
    if target.config.print and files_to_clean:
        print(f'Cleaning {len(files_to_clean)} intermediate files in {target.build_dir()}')
        for file in files_to_clean:
            os.remove(file)


def export_libs(target, path, pattern_substrings, src_dir, order):
    root_path = target_root_path(target, path, src_dir)
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
    target.exported_libs = get_unique_basenames(target.exported_libs)
    return len(target.exported_libs) > 0


def export_asset(target, asset, category=None, src_dir=True):
    full_asset = target_root_path(target, asset, src_dir)
    if os.path.exists(full_asset):
        target.exported_assets.append(Asset(asset, full_asset, category))
        return True
    else:
        console(f'export_asset failed to find: {full_asset}')
        return False


def export_assets(target, assets_path, pattern_substrings, category=None, src_dir=True):
    assets_path += '/'
    assets = glob_with_name_match(target_root_path(target, assets_path, src_dir), pattern_substrings, match_dirs=False)
    if assets:
        for full_asset in assets:
            target.exported_assets.append(Asset(assets_path, full_asset, category))
        return True
    return False


def find_syslib(target, name, apt, required):
    if target.ios or target.macos:
        if not name.startswith('-framework '):
            raise EnvironmentError(f'Expected "-framework name" but got "{name}"')
        return name # '-framework Foundation'
    elif target.linux:
        for candidate in [
            lambda: f'/usr/lib/x86_64-linux-gnu/{name}',
            lambda: f'/usr/lib/x86_64-linux-gnu/lib{name}.so',
            lambda: f'/usr/lib/x86_64-linux-gnu/lib{name}.a',
            lambda: f'/usr/lib/lib{name}.so',
            lambda: f'/usr/lib/lib{name}.a' ]:
            if os.path.isfile(candidate()):
                return candidate()
        if not required: return False
        if apt: raise IOError(f'Error {target.name} failed to find REQUIRED SysLib: {name}  Try `sudo apt install {apt}`')
        raise IOError(f'Error {target.name} failed to find REQUIRED SysLib: {name}  Try installing it with apt.')
    else:
        return name # just export it. expect system linker to find it.


def export_syslib(target, name, apt, required):
    lib = find_syslib(target, name, apt, required)
    #console(f'Exporting syslib: {name}:{lib}')
    target.exported_syslibs.append(lib)
    target.exported_syslibs = get_unique_basenames(target.exported_syslibs)
    return True


def get_lib_basename(syslib):
    if syslib.startswith('-framework '):
        return syslib
    return os.path.basename(syslib)


def reload_syslibs(target, syslibs):
    reloaded = []
    for syslib in syslibs:
        if syslib.startswith('-framework '):
            reloaded.append(syslib)
        else:
            lib = find_syslib(target, os.path.basename(syslib), apt=None, required=True)
            reloaded.append(lib)
    target.exported_syslibs = reloaded
