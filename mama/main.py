#!/usr/bin/python3.6
import sys, os, importlib
from .system import console
from .build_config import BuildConfig
from .build_target import BuildTarget
from .build_dependency import BuildDependency

def print_usage():
    console('mama [actions...] [args...]')
    console('  actions:')
    console('    build     - update, configure and build main project or specific target')
    console('    clean     - clean main project or specific target')
    console('    rebuild   - clean, update, configure and build main project or specific target')
    console('    configure - run CMake configuration on main project or specific target')
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
    console('    release   - (default) CMake configuration RelWithDebInfo')
    console('    debug     - CMake configuration Debug')
    console('    jobs=N    - Max number of parallel compilations. (default=system.core.count)')
    console('    target=P  - Name of the target')
    console('  examples:')
    console('    mama build                    Update and build main project only.')
    console('    mama clean                    Cleans main project only.')
    console('    mama rebuild                  Cleans, update and build main project only.')
    console('    mama build target=dep1        Update and build dep1 only.')
    console('    mama configure                Run CMake configuration on main project only.')
    console('    mama configure target=all     Run CMake configuration on main project and all deps.')
    console('    mama reclone target=dep1      Wipe target dependency completely and clone again.')
    console('    mama test                     Run tests on main project.')
    console('    mama test target=dep1         Run tests on target dependency project.')
    console('  environment:')
    console('    setenv("NINJA")               Path to NINJA build executable')
    console('    setenv("ANDROID_HOME")        Path to Android SDK if auto-detect fails')


def main():
    console(f'========= Mama Build Tool ==========\n')
    if sys.version_info < (3, 6):
        console('FATAL ERROR: MamaBuild requires Python 3.6')
        exit(-1)

    if len(sys.argv) == 1:
        print_usage()
        sys.exit(-1)

    config = BuildConfig(sys.argv[1:])
    source_dir = os.getcwd()
    name = os.path.basename(source_dir)
    main_dependency = BuildDependency(name, config, BuildTarget, src=source_dir)
    main_dependency.target.build_target()
