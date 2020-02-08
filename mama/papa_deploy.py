import os, shutil
from .util import write_text_to, console, glob_with_name_match, copy_dir, copy_if_needed
from typing import List


class Asset:
    def __init__(self, relpath, fullpath, category):
        """
        Creates an asset. If category is set, then relpath is ignored during deploy
            relpath  -- Relative path to source
            fullpath -- Single full path
            category -- Deployment category
        """
        reldir = os.path.dirname(relpath)
        self.name     = os.path.basename(fullpath)
        self.outpath  = fullpath[fullpath.find(reldir) + len(reldir):].lstrip('\\/')
        self.srcpath  = fullpath

        if category: self.outpath = f'{category}/{self.outpath}'
        else:        self.outpath = f'{reldir}/{self.outpath}'
        #console(f'asset {self.outpath}')

    def __str__(self):  return self.outpath
    def __repr__(self): return self.outpath


def _is_a_dynamic_library(lib):
    return lib.endswith('.dll')    or lib.endswith('.pdb') \
        or lib.endswith('.dylib')  or lib.endswith('.so')  \
        or lib.endswith('.bundle') or lib.endswith('.framework') \
        or lib.endswith('.aar')


def _gather_includes(target, recurse):
    includes = []
    def append_includes(target):
        nonlocal includes, recurse
        for lib in target.exported_libs:
            if _is_a_dynamic_library(lib):
                for include in target.exported_includes:
                    includes.append((target,include))
                break
        if recurse:
            for child in target.dep.children:
                append_includes(child.target)
    append_includes(target)
    return includes


def _gather_dylibs(target, recurse):
    dylibs = []
    def append_dylibs(target):
        nonlocal dylibs, recurse
        for lib in target.exported_libs:
            if _is_a_dynamic_library(lib):
                dylibs.append((target,lib))
        if recurse:
            for child in target.dep.children:
                append_dylibs(child.target)
    append_dylibs(target)
    return dylibs


def _gather_syslibs(target, recurse):
    syslibs = []
    def append_syslibs(target):
        nonlocal syslibs, recurse
        for lib in target.exported_syslibs:
            if not lib in syslibs:
                syslibs.append((target,lib))
        if recurse:
            for child in target.dep.children:
                append_syslibs(child.target)
    append_syslibs(target)
    return syslibs


def _gather_assets(target, recurse):
    assets = []
    def append_assets(target):
        nonlocal assets, recurse
        for asset in target.exported_assets:
            assets.append((target,asset))
        if recurse:
            for child in target.dep.children:
                append_assets(child.target)
    append_assets(target)
    return assets


def papa_deploy_to(target, package_full_path, r_includes, r_dylibs, r_syslibs, r_assets):
    config = target.config
    detail_echo = config.print and config.target_matches(target.name) and (not config.test)
    if detail_echo: console(f'  - PAPA Deploy {package_full_path}')

    includes = _gather_includes(target, r_includes)
    libs     = _gather_dylibs(target, r_dylibs)
    syslibs  = _gather_syslibs(target, r_syslibs)
    assets   = _gather_assets(target, r_assets)

    if not os.path.exists(package_full_path): # check to avoid Access Denied errors
        os.makedirs(package_full_path, exist_ok=True)
    
    descr = [f'P {os.path.basename(package_full_path)}']
    relincludes = []
    for inctarget, include in includes:
        relpath = os.path.basename(include)
        if not relpath in relincludes:
            descr.append(f'I {relpath}')
            relincludes.append(relpath)
        if detail_echo: console(f'    I ({inctarget.name+")": <16}  {relpath}')
        copy_dir(include, package_full_path)

    for libtarget, lib in libs:
        relpath = os.path.basename(lib) # TODO: how to get a proper relpath??
        descr.append(f'L {relpath}')
        if detail_echo: console(f'    L ({libtarget.name+")": <16}  {relpath}')
        #outpath = os.path.join(package_full_path, relpath)
        outpath = package_full_path
        if config.verbose: console(f'    copy {lib} -> {outpath}')
        copy_if_needed(lib, outpath)

    for systarget, syslib in syslibs:
        descr.append(f'S {syslib}')
        if detail_echo: console(f'    S ({systarget.name+")": <16}  {syslib}')

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

