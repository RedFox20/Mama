import os, concurrent.futures
from .build_target import BuildTarget
from .build_dependency import BuildDependency
from .util import save_file_if_contents_changed
from .system import console

def get_mama_dependencies_cmake(root_dependency: BuildDependency, build:str):
    if not root_dependency.children:
        return ''
    return f'''# get MAMA_INCLUDES and MAMA_LIBS for this platform; verbose for CLion CMake parser
    include("{root_dependency.dep_dir}/{build}/mama-dependencies.cmake")'''

def save_generic_cmake(root_dependency: BuildDependency):
    outfile = f'{root_dependency.src_dir}/mama.cmake'
    # note: we save verbose include directives, because CLion has a hard time detecting macro paths
    text = f'''
# This file is auto-generated by mama build. Do not modify by hand!
if(ANDROID OR ANDROID_NDK)
    set(MAMA_BUILD "android")
    {get_mama_dependencies_cmake(root_dependency, 'android')}
elseif(WIN32)
    set(MAMA_BUILD "windows")
    {get_mama_dependencies_cmake(root_dependency, 'windows')}
elseif(APPLE)
  if(IOS_PLATFORM)
    set(IOS TRUE)
    set(MAMA_BUILD "ios")
    {get_mama_dependencies_cmake(root_dependency, 'ios')}
  else()
    set(MACOS TRUE)
    set(MAMA_BUILD "macos")
    {get_mama_dependencies_cmake(root_dependency, 'macos')}
  endif()
elseif(UNIX)
    set(LINUX TRUE)
    set(MAMA_BUILD "linux")
    {get_mama_dependencies_cmake(root_dependency, 'linux')}
else()
    message(FATAL_ERROR "mama build: Unsupported Platform!")
    set(MAMA_BUILD "???")
endif()
if(CMAKE_CXX_COMPILER_ID MATCHES "Clang")
    set(CLANG TRUE)
elseif(CMAKE_CXX_COMPILER_ID MATCHES "GNU")
    set(GCC TRUE)
endif()
'''
    save_file_if_contents_changed(outfile, text)


def get_cmake_path_list(paths):
    pathlist = '' 
    for path in paths: pathlist += f'\n    "{path}"'
    return pathlist


def save_dependencies_cmake(root_dependency: BuildDependency):
    if not root_dependency.build_dir_exists():
        return # probably CLEAN, so nothing to save
    outfile = f'{root_dependency.build_dir}/mama-dependencies.cmake'
    if not root_dependency.children:
        if os.path.exists(outfile):
            os.remove(outfile) # no more deps, get rid of the dependency file
        return
    text = '''
# This file is auto-generated by mama build. Do not modify by hand!
set(MAMA_INCLUDES "")
set(MAMA_LIBS     "")
'''
    for dep in root_dependency.children:
        includes  = get_cmake_path_list(dep.target.exported_includes)
        libraries = get_cmake_path_list(dep.target.exported_libs)
        text += f'''
# Package {dep.name}
set({dep.name}_INCLUDES {includes})
set({dep.name}_LIBS {libraries})
set(MAMA_INCLUDES ${{MAMA_INCLUDES}} ${{{dep.name}_INCLUDES}})
set(MAMA_LIBS     ${{MAMA_LIBS}}     ${{{dep.name}_LIBS}})
'''
    save_file_if_contents_changed(outfile, text)


def create_mama_cmake_includes(root_dependency: BuildDependency):
    save_dependencies_cmake(root_dependency)
    save_generic_cmake(root_dependency)


def load_child_dependencies(root_dependency: BuildDependency, parallel=True):
    changed = False
    if parallel:
        futures = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as e:
            for dep in root_dependency.children:
                futures.append(e.submit(load_dependency_chain, dep))
        for f in futures:
            changed |= f.result()
    else:
        for dep in root_dependency.children:
            changed |= load_dependency_chain(dep)
    return changed


def load_dependency_chain(dep: BuildDependency):
    if dep.already_loaded:
        return dep.should_rebuild

    changed = dep.load()
    changed |= load_child_dependencies(dep)
    return changed


def execute_task_chain(root_dependency: BuildDependency):
    for dep in root_dependency.children:
        execute_task_chain(dep)
    
    create_mama_cmake_includes(root_dependency)
    root_dependency.target.execute_tasks()

def find_dependency(root_dependency: BuildDependency, name) -> BuildDependency:
    if root_dependency.name == name:
        return root_dependency
    for dep in root_dependency.children:
        found = find_dependency(dep, name)
        if found: return found
    return None
