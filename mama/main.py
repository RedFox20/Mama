#!/usr/bin/python3.6
import sys, os, importlib
from .system import console
from .build_config import BuildConfig
from .build_target import BuildTarget
from .build_dependency import BuildDependency
from .dependency_chain import load_dependency_chain, execute_task_chain

def print_usage():
    console('mama [actions...] [args...]')
    console('  actions:')
    console('    build     - update, configure and build main project or specific target')
    console('    clean     - clean main project or specific target')
    console('    rebuild   - clean, update, configure and build main project or specific target')
    console('    configure - run CMake configuration on main project or specific target')
    console('    update    - update specific target dependency by calling git pull')
    console('    reclone   - wipe specific target dependency and clone it again')
    console('    test      - run tests for main project or specific target')
    console('    add       - add new dependency')
    console('    new       - create new mama build file')
    console('  args:')
    console('    windows   - build for windows')
    console('    linux     - build for linux')
    console('    macos     - build for macos')
    console('    ios       - build for ios')
    console('    android   - build for android')
    console('    clang     - prefer clang for linux (default on linux/macos/ios/android)')
    console('    gcc       - prefer gcc for linux')
    console('    android   - build for android')
    console('    release   - (default) CMake configuration RelWithDebInfo')
    console('    debug     - CMake configuration Debug')
    console('    jobs=N    - Max number of parallel compilations. (default=system.core.count)')
    console('    target=P  - Name of the target')
    console('  examples:')
    console('    mama build                    Update and build main project only.')
    console('    mama clean                    Cleans main project only.')
    console('    mama rebuild                  Cleans, update and build main project only.')
    console('    mama update build             Update all dependencies and then build.')
    console('    mama build target=dep1        Update and build dep1 only.')
    console('    mama configure                Run CMake configuration on main project only.')
    console('    mama configure target=all     Run CMake configuration on main project and all deps.')
    console('    mama reclone target=dep1      Wipe target dependency completely and clone again.')
    console('    mama test                     Run tests on main project.')
    console('    mama test target=dep1         Run tests on target dependency project.')
    console('  environment:')
    console('    setenv("NINJA")               Path to NINJA build executable')
    console('    setenv("ANDROID_HOME")        Path to Android SDK if auto-detect fails')

# preload actions only valid for root_dependency
def run_preload_actions(config: BuildConfig, root_dependency: BuildDependency):
    if config.clean and not config.target:
        root_dependency.clean()

def run_load_actions(config: BuildConfig, root_dependency: BuildDependency):
    load_dependency_chain(root_dependency)

def run_postload_actions(config: BuildConfig, root_dependency: BuildDependency):
    execute_task_chain(root_dependency)

def main():
    console(f'========= Mama Build Tool ==========')
    if sys.version_info < (3, 6):
        console('FATAL ERROR: MamaBuild requires Python 3.6')
        exit(-1)

    if len(sys.argv) == 1:
        print_usage()
        sys.exit(-1)

    config = BuildConfig(sys.argv[1:])
    source_dir = os.getcwd()
    name = os.path.basename(source_dir)
    root_dependency = BuildDependency(name, config, BuildTarget, src=source_dir, is_root=True)

    if config.update:
        if not config.target: config.target = 'all'
        console(f'Updating {config.target} target')

    if config.rebuild:
        config.build = True
        config.clean = True

    run_preload_actions(config, root_dependency)
    run_load_actions(config, root_dependency)
    run_postload_actions(config, root_dependency)
