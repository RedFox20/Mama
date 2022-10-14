import os, concurrent.futures
from typing import List
from .build_dependency import BuildDependency
from .util import save_file_if_contents_changed
from .system import console


def _get_cmake_path_list(paths):
    pathlist = '' 
    for path in paths: pathlist += f'\n    "{path}"'
    return pathlist


def _get_exported_libs(target):
    filtered = []
    allowed = []
    if target.windows:
        allowed = ['.lib']
    elif target.android:
        allowed = ['.a', '.so']
    elif target.linux: # TODO: android builds on Linux are impossible with this approach 
        allowed = ['.a', '.so']
    elif target.macos:
        allowed = ['.a', '.dylib', '.bundle']
    elif target.ios:
        allowed = ['.a', '.dylib', '.framework']
    elif target.raspi or target.oclea:
        allowed = ['.a', '.so']

    #print(f'{target.name: <16} exported: {target.exported_libs}')
    for lib in target.exported_libs:
        for ext in allowed:
            if lib.endswith(ext):
                filtered.append(lib)
    #print(f'{target.name: <16} filtered: {filtered}')
    return filtered


def _get_hierarchical_libs(root: BuildDependency):
    deps = []
    syslibs = []
    def add_deps(dep: BuildDependency):
        nonlocal deps, syslibs
        deps += _get_exported_libs(dep.target)
        syslibs += dep.target.exported_syslibs
        for child in dep.get_children():
            add_deps(child)
    add_deps(root)
    return deps + syslibs


def _get_flattened_deps(root: BuildDependency):
    # deps have to be sorted in [parent] [child] order for Unix linkers
    ordered = []
    def add_unique_items(deps: List[BuildDependency]):
        for child in deps:
            if child in ordered: # already in deps list, so we need to move it lower
                ordered.remove(child)
            ordered.append(child)
            add_unique_items(child.get_children())
    add_unique_items(root.get_children())
    return ordered


def get_full_flattened_deps(root: BuildDependency):
    """ Information list of dep names """
    deps = [root] + _get_flattened_deps(root)
    return [dep.name for dep in deps]


def _get_mama_dependencies_cmake(root: BuildDependency, build:str):
    if not root.get_children():
        return ''
    return f'include("{root.dep_dir}/{build}/mama-dependencies.cmake")'


def _mama_cmake_path(root: BuildDependency):
    if not root.src_dir: # for artifactory pkgs, there is no src_dir
        return f'{root.build_dir}/mama.cmake'
    return f'{root.src_dir}/mama.cmake'


def _save_mama_cmake_and_dependencies_cmake(root: BuildDependency):
    # save the {build}/mama-dependencies.cmake
    _save_dependencies_cmake(root)
    # the following is the proxy `mysource/mama.cmake` file
    # which will reference each mama-dependencies.cmake depending on platform
    _save_mama_cmake(root)


def _get_dependency_cmake_defines(dep: BuildDependency):
    name = dep.name
    own_libs = _get_exported_libs(dep.target) + dep.target.exported_syslibs
    all_libs = _get_hierarchical_libs(dep)

    includes = _get_cmake_path_list(dep.target.exported_includes)
    own_libs_list = _get_cmake_path_list(own_libs)
    all_libs_list = _get_cmake_path_list(all_libs)

    # reference name_LIB if it equals name_LIBS
    if own_libs_list == all_libs_list:
        all_libs_list = f'${{{name}_LIB}}'
    #console(f'{name} own_libs: {own_libs_list}')
    #console(f'{name} all_libs: {all_libs_list}')
    return f'${{{name}_INCLUDES}}', \
f'''
# Package {name}
set({name}_INCLUDES {includes})
# only {name} libs
set({name}_LIB {own_libs_list})
# includes {name} libs and all dependency libs
set({name}_LIBS {all_libs_list})
'''


def _save_dependencies_cmake(root: BuildDependency):
    if not root.build_dir_exists():
        return # probably CLEAN, so nothing to save
    outfile = f'{root.build_dir}/mama-dependencies.cmake'
    text = \
'''
# This file is auto-generated by mama build. Do not modify by hand!
'''
    includes_defs = []
    root.flattened_deps = [root] + _get_flattened_deps(root)
    for dep in root.flattened_deps:
        includes_def, package_text = _get_dependency_cmake_defines(dep)
        includes_defs.append(includes_def)
        text += package_text

    # and finally, set the MAMA_INCLUDES and MAMA_LIBS
    includes = ' '.join(includes_defs)
    libs = f'${{{root.name}_LIBS}}' # use the root package to get the full flat list of deps
    text += \
f'''
set(MAMA_INCLUDES ${{MAMA_INCLUDES}} {includes})
set(MAMA_LIBS     ${{MAMA_LIBS}}     {libs})
'''

    save_file_if_contents_changed(outfile, text)


def _save_mama_cmake(root: BuildDependency):
    # note: we save verbose include directives, 
    #       because CLion has a hard time detecting macro paths
    text = \
f'''
# This file is auto-generated by mama build. Do not modify by hand!
if(CMAKE_CXX_COMPILER_ID MATCHES "Clang")
    set(CLANG TRUE)
elseif(CMAKE_CXX_COMPILER_ID MATCHES "GNU")
    set(GCC TRUE)
endif()
# Initializes the INCLUDE and LIBS, they will overwritten in mama-dependencies.cmake
set(MAMA_INCLUDE "")
set(MAMA_LIBS "")
# Set MAMA_INCLUDES and MAMA_LIBS for each platform
if(ANDROID OR ANDROID_NDK)
    set(MAMA_BUILD "android")
    {_get_mama_dependencies_cmake(root, 'android')}
elseif(WIN32)
    if(MAMA_ARCH_X64)
        set(MAMA_BUILD "windows")
        {_get_mama_dependencies_cmake(root, 'windows')}
    elseif(MAMA_ARCH_X86)
        set(MAMA_BUILD "windows32")
        {_get_mama_dependencies_cmake(root, 'windows32')}
    elseif(CMAKE_GENERATOR_PLATFORM MATCHES "ARM64")
        set(MAMA_BUILD "winarm")
        {_get_mama_dependencies_cmake(root, 'winarm')}
    elseif(CMAKE_GENERATOR_PLATFORM MATCHES "ARM")
        set(MAMA_BUILD "winarm32")
        {_get_mama_dependencies_cmake(root, 'winarm32')}
    else()
        message(FATAL_ERROR "MAMA: Unrecognized target architecture ${{CMAKE_GENERATOR_PLATFORM}}")
    endif()
elseif(APPLE)
  if(IOS_PLATFORM)
    set(IOS TRUE)
    set(MAMA_BUILD "ios")
    # Always arm64
    {_get_mama_dependencies_cmake(root, 'ios')}
  else()
    set(MACOS TRUE)
    set(MAMA_BUILD "macos")
    # Always x64
    {_get_mama_dependencies_cmake(root, 'macos')}
  endif()
elseif(RASPI)
    set(MAMA_BUILD "raspi")
    # Always armv7
    {_get_mama_dependencies_cmake(root, 'raspi32')}
elseif(OCLEA)
    set(MAMA_BUILD "oclea")
    # Always arm64
    {_get_mama_dependencies_cmake(root, 'oclea64')}
elseif(UNIX)
    set(LINUX TRUE)
    if(MAMA_ARCH_X64)
        set(MAMA_BUILD "linux")
        {_get_mama_dependencies_cmake(root, 'linux')}
    elseif(MAMA_ARCH_X86)
        set(MAMA_BUILD "linux32")
        {_get_mama_dependencies_cmake(root, 'linux32')}
    else()
        message(FATAL_ERROR "MAMA: Unrecognized target architecture")
    endif()
else()
    message(FATAL_ERROR "mama build: Unsupported Platform!")
    set(MAMA_BUILD "???")
endif()

# Overrides linkage on MSVC to non-debug run-time library (TODO: make this configurable)
if(MSVC)
    add_definitions(-D_ITERATOR_DEBUG_LEVEL=0)
    foreach(MODE "_DEBUG" "_MINSIZEREL" "_RELEASE" "_RELWITHDEBINFO")
        string(REPLACE "/MDd" "/MD" TMP "${{CMAKE_C_FLAGS${{MODE}}}}")
        set(CMAKE_C_FLAGS${{MODE}} "${{TMP}}" CACHE STRING "" FORCE)
        string(REPLACE "/MDd" "/MD" TMP "${{CMAKE_CXX_FLAGS${{MODE}}}}")
        set(CMAKE_CXX_FLAGS${{MODE}} "${{TMP}}" CACHE STRING "" FORCE)
    endforeach(MODE)
endif()
'''
    save_file_if_contents_changed(_mama_cmake_path(root), text)


def load_dependency_chain(root: BuildDependency):
    """
    This is main entrypoint for building the dependency chain.
    All dependencies must be resolved at this stage
    """
    with concurrent.futures.ThreadPoolExecutor() as e:
        def load_dependency(dep: BuildDependency):
            if dep.already_loaded:
                return dep.should_rebuild
            changed = dep.load()
            futures = []
            for child in dep.get_children():
                futures.append(e.submit(load_dependency, child))
            for f in futures:
                changed |= f.result()
            dep.after_load()
            return changed
        load_dependency(root)


def execute_task_chain(root: BuildDependency):
    if root.already_executed:
        return

    if not os.path.exists(_mama_cmake_path(root)):
        _save_mama_cmake_and_dependencies_cmake(root) # save a dummy mama.cmake before build

    for dep in root.get_children():
        execute_task_chain(dep)

    if root.already_executed:
        print(f"Critical Error: '{root.name}' executed by child project")
        raise RuntimeError(f"Cyclical Dependency detected for '{root.name}'")

    _save_mama_cmake_and_dependencies_cmake(root)
    root.target._execute_tasks()

    if root.config.verbose and root.is_root_or_config_target():
        names = [dep.name for dep in root.flattened_deps]
        dep_names = " ".join(names) if root.flattened_deps else '<none>'
        console(f'  - {root.name} Dependencies:  {dep_names}')
        
        all_deps = [root] + root.flattened_deps
        libs = []
        for dep in all_deps:
            libs += [(dep.name, 'L', lib) for lib in dep.target.exported_libs]
            libs += [(dep.name, 'S', lib) for lib in dep.target.exported_syslibs]

        if libs:
            console(f'  - {root.name} Exported Libs:')
            for lib in libs:
                console(f'    {lib[0]} [{lib[1]}] {lib[2]}')
        else:
            console(f'  - {root.name} Exported Libs: <none>')


def find_dependency(root: BuildDependency, name) -> BuildDependency:
    """ This is mainly used for finding root target or specific command line target """
    if root.name == name:
        return root
    for dep in root.get_children():
        found = find_dependency(dep, name)
        if found: return found
    return None
