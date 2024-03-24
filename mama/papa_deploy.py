from __future__ import annotations
from typing import List, TYPE_CHECKING
import os

from .types.git import Git
from .types.local_source import LocalSource
from .types.artifactory_pkg import ArtifactoryPkg
from .types.dep_source import DepSource
from .types.asset import Asset

from .util import normalized_path, normalized_join, read_lines_from \
                , write_text_to, console, copy_if_needed

import mama.package as package

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


def _gather_libs(target:BuildTarget, recurse):
    # gather all libs from the root target
    libs = [(target,l) for l in target.exported_libs]

    # and for children, only gather dynamic libs if recurse is set
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


def _append_includes(target:BuildTarget, package_full_path, detail_echo, descr, includes):
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

        src_dir = os.path.dirname(src_path)
        if src_dir != dst_dir:
            if config.verbose: console(f'    copy {src_path}\n      -> {dst_dir}')
            copy_if_needed(src_path, dst_dir, includes_filter)

    relincludes = []  # TODO: what was the point of this again?
    for inctarget, abs_include in includes:
        relpath = os.path.basename(abs_include)
        if not relpath in relincludes:
            relincludes.append(relpath)
            append(relpath)


def papa_deploy_to(target:BuildTarget, package_full_path:str,
                   r_includes:bool, r_dylibs:bool, 
                   r_syslibs:bool, r_assets:bool):
    config = target.config
    detail_echo = config.print and target.is_current_target() and (not config.test)
    if detail_echo: console(f'  - PAPA Deploy {package_full_path}')

    dependencies = _gather_dependencies(target)

    if not os.path.exists(package_full_path): # check to avoid Access Denied errors
        os.makedirs(package_full_path, exist_ok=True)

    # set up project and dependencies
    descr = [ f'P {target.name}' ]
    for d in dependencies:
        if detail_echo: console(f'    D {d.dep_source}')
        descr.append(f'D {d.dep_source.get_papa_string()}')

    includes = _gather_includes(target, r_includes)
    _append_includes(target, package_full_path, detail_echo, descr, includes)

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
        #outpath = package_full_path
        if detail_echo: console(f'    L ({libtarget.name+")": <16}  {relpath}')
        if lib != outpath:
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
        outpath = normalized_join(package_full_path, asset.outpath)
        if asset.srcpath != outpath:
            folder = os.path.dirname(outpath)
            if not os.path.exists(folder):
                os.makedirs(folder, exist_ok=True)
            copy_if_needed(asset.srcpath, outpath)

    write_text_to(os.path.join(package_full_path, 'papa.txt'), '\n'.join(descr))

    # write summary
    if config.print:
        console(f'  PAPA Deployed: {len(includes)} includes, {len(libs)} libs, {len(syslibs)} syslibs, {len(assets)} assets')


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
        self.dependencies = []
        self.includes = []
        self.libs = []
        self.syslibs = []
        self.assets: List[Asset] = []

        def append_to(to:list, line):
            to.append(normalized_join(self.papa_dir, line[2:].strip()))

        for line in read_lines_from(self.papa_file):
            if   line.startswith('P '): self.project_name = line[2:].strip()
            elif line.startswith('D '): self.dependencies.append(make_dep_source(line[2:].strip()))
            elif line.startswith('I '): append_to(self.includes, line)
            elif line.startswith('L '): append_to(self.libs, line)
            elif line.startswith('S '): append_to(self.syslibs, line)
            elif line.startswith('A '):
                relpath = line[2:].strip()
                fullpath = normalized_join(self.papa_dir, relpath)
                self.assets.append(Asset(relpath, fullpath, None))

