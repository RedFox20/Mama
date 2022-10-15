import os, shutil
from typing import List

from .build_dependency import BuildDependency
from .artifactory import artifactory_archive_name, artifactory_upload_ftp
from .util import get_file_size_str, normalized_path, write_text_to, console, copy_if_needed
import mama.package as package


def _gather_dependencies(target) -> List[BuildDependency]:
    dependecies = []
    for child in target.children():
        dependecies.append(child)
    return dependecies


def _results_contain(results, contains_value):
    for target,value in results:
        if value == contains_value:
            return True
    return False


def _gather(target, recurse, results:list, get_candidates):
    for value in get_candidates(target):
        if not _results_contain(results, value):
            results.append((target,value))
    if recurse:
        for child in target.dep.children():
            _gather(child.target, True, results, get_candidates)
    return results


def _gather_includes(target, recurse):
    includes = []
    return _gather(target, recurse, includes, lambda t: t.exported_includes)


def _gather_libs(target, recurse):
    # gather all libs from the root target
    libs = [(target,l) for l in target.exported_libs]

    # and for children, only gather dynamic libs if recurse is set
    if recurse:
        def get_dylibs(t):
            for l in t.exported_libs:
                if package.is_a_dynamic_library(l): yield l
        for child in target.dep.children():
            _gather(child, recurse, libs, get_dylibs)
    return libs


def _gather_syslibs(target, recurse):
    syslibs = []
    return _gather(target, recurse, syslibs, lambda t: t.exported_syslibs)


def _gather_assets(target, recurse):
    assets = []
    return _gather(target, recurse, assets, lambda t: t.exported_assets)


def _append_includes(target, package_full_path, detail_echo, descr, includes):
    if not includes:
        return # nothing to do
    config = target.config
    includes_root = package_full_path + '/include'
    # TODO: should we include .cpp files for easier debugging?
    includes_filter = ['.h','.hpp','.hxx','.hh','.c','.cpp','.cxx']

    # set the default include
    descr.append(f'I include')

    def append(relpath):
        src_path = abs_include
        dst_dir = includes_root

        # matches default include?
        if relpath == 'include' or relpath == 'include/':
            if detail_echo: console(f'    I ({inctarget.name+")": <16}  include')
            dst_dir = normalized_path(includes_root + '/../')
        else:
            if detail_echo: console(f'    I ({inctarget.name+")": <16}  include/{relpath}')
            descr.append(f'I include/{relpath}')

        if config.verbose: console(f'    copy {src_path}\n      -> {dst_dir}')
        copy_if_needed(src_path, dst_dir, includes_filter)

    relincludes = []  # TODO: what was the point of this again?
    for inctarget, abs_include in includes:
        relpath = os.path.basename(abs_include)
        if not relpath in relincludes:
            relincludes.append(relpath)
            append(relpath)


def papa_deploy_to(target, package_full_path, r_includes, r_dylibs, r_syslibs, r_assets):
    config = target.config
    detail_echo = config.print and config.target_matches(target.name) and (not config.test)
    if detail_echo: console(f'  - PAPA Deploy {package_full_path}')

    dependencies = _gather_dependencies(target)

    if not os.path.exists(package_full_path): # check to avoid Access Denied errors
        os.makedirs(package_full_path, exist_ok=True)

    # set up project and dependencies
    descr = [ f'P {os.path.basename(package_full_path)}' ]
    for d in dependencies:
        if detail_echo: console(f'    D {d.dep_source}')
        descr.append(f'D {d.dep_source.get_papa_string()}')

    includes = _gather_includes(target, r_includes)
    _append_includes(target, package_full_path, detail_echo, descr, includes)

    libs = _gather_libs(target, r_dylibs)
    for libtarget, lib in libs:
        relpath = package.get_lib_basename(lib) # TODO: how to get a proper relpath??
        descr.append(f'L {relpath}')
        #outpath = os.path.join(package_full_path, relpath)
        outpath = package_full_path
        if detail_echo: console(f'    L ({libtarget.name+")": <16}  {relpath}')
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
        outpath = os.path.join(package_full_path, asset.outpath)

        folder = os.path.dirname(outpath)
        if not os.path.exists(folder):
            os.makedirs(folder, exist_ok=True)
        copy_if_needed(asset.srcpath, outpath)

    write_text_to(os.path.join(package_full_path, 'papa.txt'), '\n'.join(descr))

    # write summary
    if config.print:
        console(f'  PAPA Deployed: {len(includes)} includes, {len(libs)} libs, {len(syslibs)} syslibs, {len(assets)} assets')


def papa_upload_to(target, package_full_path):
    """
    - target: Target which was configured and packaged
    - package_full_path: Full path to deployed PAPA package
    """
    config = target.config
    dst_dir = target.build_dir()
    archive_name = artifactory_archive_name(target)
    if config.verbose:
        console(f'    archiving {package_full_path}\n {"":10}-> {dst_dir}/{archive_name}.zip')

    archive = shutil.make_archive(archive_name, 'zip', package_full_path, '.', verbose=True)
    archive_path = dst_dir + '/' + os.path.basename(archive)
    if os.path.exists(archive_path):
        os.remove(archive_path)
    shutil.move(archive, archive_path)

    if config.print:
        size = os.path.getsize(archive_path)
        console(f'  - PAPA Upload {archive_name}  {get_file_size_str(size)}')
    artifactory_upload_ftp(target, archive_path)

    if config.verbose:
        console(f'  PAPA Uploaded {os.path.basename(archive)}')
