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
    return lib.endswith('.dll') or lib.endswith('.so') \
        or lib.endswith('.dylib') or lib.endswith('.bundle') \
        or lib.endswith('.framework')


def _recurse_includes(target):
    includes = []
    def append_includes(target):
        nonlocal includes
        for lib in target.exported_libs:
            if _is_a_dynamic_library(lib):
                includes += target.exported_includes
                break
        for child in target.dep.children:
            append_includes(child.target)
    append_includes(target)
    return includes


def _recurse_dylibs(target):
    dylibs = []
    def append_dylibs(target):
        nonlocal dylibs
        for lib in target.exported_libs:
            if _is_a_dynamic_library(lib):
                dylibs.append(lib)
        for child in target.dep.children:
            append_dylibs(child.target)
    append_dylibs(target)
    return dylibs


def _recurse_syslibs(target):
    syslibs = []
    def append_syslibs(target):
        nonlocal syslibs
        for lib in target.exported_syslibs:
            if not lib in syslibs:
                syslibs.append(lib)
        for child in target.dep.children:
            append_syslibs(child.target)
    append_syslibs(target)
    return syslibs


def _recurse_assets(target):
    assets = []
    def append_assets(target):
        nonlocal assets
        assets += target.exported_assets
        for child in target.dep.children:
            append_assets(child.target)
    append_assets(target)
    return assets


def papa_deploy_to(target, package_full_path, r_includes, r_dylibs, r_syslibs, r_assets):
    config = target.config
    if config.print: console(f'  - PAPA Deploy {package_full_path}')

    includes = _recurse_includes(target) if r_includes else target.exported_includes
    libs     = _recurse_dylibs(target)   if r_dylibs   else target.exported_libs
    syslibs  = _recurse_syslibs(target)  if r_syslibs  else target.exported_syslibs
    assets   = _recurse_assets(target)   if r_assets   else target.exported_assets

    if not os.path.exists(package_full_path): # check to avoid Access Denied errors
        os.makedirs(package_full_path, exist_ok=True)
    
    descr = [f'P {os.path.basename(package_full_path)}']
    relincludes = []
    for include in includes:
        relpath = os.path.basename(include)
        if not relpath in relincludes:
            descr.append(f'I {relpath}')
            relincludes.append(relpath)
        if config.print:
            parent = os.path.split(include)[0]
            parent = os.path.basename(parent)
            console(f'    I {relpath}  ({parent})')
        copy_dir(include, package_full_path)

    for lib in libs:
        relpath = os.path.basename(lib) # TODO: how to get a proper relpath??
        descr.append(f'L {relpath}')
        if config.print: console(f'    L {relpath}')
        outpath = os.path.join(package_full_path, relpath)
        copy_if_needed(lib, outpath)

    for syslib in syslibs:
        descr.append(f'S {syslib}')
        if config.print: console(f'    S {syslib}')

    for asset in assets:
        descr.append(f'A {asset.outpath}')
        if config.print: console(f'    A {asset.outpath}')
        outpath = os.path.join(package_full_path, asset.outpath)

        folder = os.path.dirname(outpath)
        if not os.path.exists(folder):
            os.makedirs(folder, exist_ok=True)
        
        copy_if_needed(asset.srcpath, outpath)

    write_text_to(os.path.join(package_full_path, 'papa.txt'), '\n'.join(descr))

