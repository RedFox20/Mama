import os, shutil
from .util import write_text_to, console, glob_with_name_match
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


def _should_copy(src, dst):
    if not os.path.exists(dst):
        return True

    src_stat = os.stat(src)
    dst_stat = os.stat(dst)

    #if src_stat.st_mtime != dst_stat.st_mtime or \
    if src_stat.st_size != dst_stat.st_size:
        #console(f'copy {src}\n --> {dst}')
        return True
    #console(f'skip {dst}')
    return False
    

def _copy_if_needed(src, dst):
    if _should_copy(src, dst):
        #console(f'copy {src}\n --> {dst}')
        shutil.copy2(src, dst)


def _copy_include_dir(package_full_path, include):
    root = os.path.dirname(include)
    for fulldir, _, files in os.walk(include):
        reldir = fulldir[len(root):].lstrip('\\/')
        if reldir:
            dst_folder = os.path.join(package_full_path, reldir)
            os.makedirs(dst_folder, exist_ok=True)
        for file in files:
            src_file = os.path.join(fulldir, file)
            dst_file = os.path.join(dst_folder, file)
            _copy_if_needed(src_file, dst_file)


def papa_deploy_to(package_full_path, includes, libs, syslibs, assets: List[Asset]):
    console(f'  - PAPA Deploy {package_full_path}')

    if not os.path.exists(package_full_path): # check to avoid Access Denied errors
        os.makedirs(package_full_path, exist_ok=True)
    
    descr = [f'P {os.path.basename(package_full_path)}']
    
    for include in includes:
        relpath = os.path.basename(include)
        descr.append(f'I {relpath}')
        #console(f'    I {relpath}')
        _copy_include_dir(package_full_path, include)

    for lib in libs:
        relpath = os.path.basename(lib) # TODO: how to get a proper relpath??
        descr.append(f'L {relpath}')
        #console(f'    L {relpath}')
        outpath = os.path.join(package_full_path, relpath)
        _copy_if_needed(lib, outpath)

    for syslib in syslibs:
        descr.append(f'S {syslib}')
        #console(f'    S {syslib}')

    for asset in assets:
        descr.append(f'A {asset.outpath}')
        #console(f'    A {asset.outpath}')
        outpath = os.path.join(package_full_path, asset.outpath)

        folder = os.path.dirname(outpath)
        if not os.path.exists(folder):
            os.makedirs(folder, exist_ok=True)
        
        _copy_if_needed(asset.srcpath, outpath)

    write_text_to(os.path.join(package_full_path, 'papa.txt'), '\n'.join(descr))

