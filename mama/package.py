import os
from mama.system import console
from mama.util import normalized_path, glob_with_name_match
from mama.papa_deploy import Asset


def target_root_path(target, path, src_dir):
    root = target.dep.src_dir if src_dir else target.dep.build_dir
    return normalized_path(os.path.join(root, path))


def _get_unique_basenames(items):
    unique = dict()
    for item in items:
        if isinstance(item, tuple):
            unique[os.path.basename(item[0])] = item
        elif item.startswith('-framework '):
            unique[item.split(' ', 1)[1]] = item
        else:
            unique[os.path.basename(item)] = item
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
        target.exported_libs = _get_unique_basenames(target.exported_libs)
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


def export_libs(target, path, pattern_substrings, src_dir, order):
    libs = glob_with_name_match(target_root_path(target, path, src_dir), pattern_substrings)
    libs = cleanup_libs_list(libs)
    if order:
        def lib_index(lib):
            for i in range(len(order)):
                if order[i] in lib: return i
            return len(order)  # if this lib name does not match, put it at the end of the list
        def sort_key(lib):
            return lib_index(lib)
        libs.sort(key=sort_key)
    target.exported_libs += libs
    target.exported_libs = _get_unique_basenames(target.exported_libs)
    return len(target.exported_libs) > 0


def export_asset(target, asset, category, src_dir):
    full_asset = target_root_path(target, asset, src_dir)
    if os.path.exists(full_asset):
        target.exported_assets.append(Asset(asset, full_asset, category))
        return True
    else:
        console(f'export_asset failed to find: {full_asset}')
        return False


def export_assets(target, assets_path, pattern_substrings, category, src_dir):
    assets_path += '/'
    assets = glob_with_name_match(target_root_path(target, assets_path, src_dir), pattern_substrings, match_dirs=False)
    if assets:
        for full_asset in assets:
            target.exported_assets.append(Asset(assets_path, full_asset, category))
        return True
    return False


def export_syslib(target, name, apt, required):
    if target.ios or target.macos:
        if not name.startswith('-framework '):
            raise EnvironmentError(f'Expected "-framework name" but got "{name}"')
        lib = name
    elif target.linux:
        lib = f'/usr/lib/x86_64-linux-gnu/{name}'
        if not os.path.isfile(lib): lib = f'/usr/lib/x86_64-linux-gnu/lib{name}.so'
        if not os.path.isfile(lib): lib = f'/usr/lib/x86_64-linux-gnu/lib{name}.a'
        if not os.path.isfile(lib): lib = f'/usr/lib/lib{name}.so'
        if not os.path.isfile(lib): lib = f'/usr/lib/lib{name}.a'
        if not os.path.isfile(lib):
            if not required: return False
            if apt:
                raise IOError(f'Error {target.name} failed to find REQUIRED SysLib: {name}  Try `sudo apt install {apt}`')
            raise IOError(f'Error {target.name} failed to find REQUIRED SysLib: {name}  Try installing it with apt.')
    else:
        lib = name # just export it. expect system linker to find it.
    #console(f'Exporting syslib: {name}:{lib}')
    target.exported_syslibs.append(lib)
    target.exported_syslibs = _get_unique_basenames(target.exported_syslibs)
    return True

