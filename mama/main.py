#!/usr/bin/python3.6
import sys, os, importlib
from .system import console, execute
from .util import glob_with_extensions, glob_folders_with_name_match
from .build_config import BuildConfig
from .build_target import BuildTarget
from .build_dependency import BuildDependency
from .dependency_chain import load_dependency_chain, execute_task_chain, find_dependency

def print_usage():
    console('mama [actions...] [args...]')
    console('  actions:')
    console('    build      - update, configure and build main project or specific target')
    console('    clean      - clean main project or specific target')
    console('    rebuild    - clean, update, configure and build main project or specific target')
    console('    configure  - run CMake configuration on main project or specific target')
    console('    update     - update specific target dependency by calling git pull')
    console('    reclone    - wipe specific target dependency and clone it again')
    console('    test       - run tests for main project or specific target')
    console('    add        - add new dependency')
    console('    new        - create new mama build file')
    console('    open=<tgt> - open a project file')
    console('    help       - shows this help list')
    console('  args:')
    console('    windows    - build for windows')
    console('    linux      - build for linux')
    console('    macos      - build for macos')
    console('    ios        - build for ios')
    console('    android    - build for android')
    console('    clang      - prefer clang for linux (default on linux/macos/ios/android)')
    console('    gcc        - prefer gcc for linux')
    console('    fortran    - enable automatic fortran detection (or configure this in mamafile)')
    console('    release    - (default) CMake configuration RelWithDebInfo')
    console('    debug      - CMake configuration Debug')
    console('    jobs=N     - Max number of parallel compilations. (default=system.core.count)')
    console('    target=P   - Name of the target')
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
    console('    mama test=arg                 Run tests on main project with an argument.')
    console('    mama test="arg1 arg2"         Run tests on main project with multiple arguments.')
    console('    mama test target=dep1         Run tests on target dependency project.')
    console('  environment:')
    console('    setenv("NINJA")               Path to NINJA build executable')
    console('    setenv("ANDROID_HOME")        Path to Android SDK if auto-detect fails')


def open_project(config: BuildConfig, root_dependency: BuildDependency):
    found = root_dependency if config.open == 'root' else find_dependency(root_dependency, config.open)
    if not found:
        raise KeyError(f'No project named {config.open}')
    
    if config.windows:
        solutions = glob_with_extensions(found.build_dir, ['.sln'])
        if not solutions:
            raise EnvironmentError('Could not find any Visual Studio solutions!')
        execute(f'start {solutions[0]}', echo=True)

    elif config.macos or config.ios:
        projects = glob_folders_with_name_match(found.build_dir, ['.xcodeproj'])
        if not solutions:
            raise EnvironmentError('Could not find any Xcode projects!')
        execute(f'open {projects[0]}', echo=True)

    elif config.linux:
        raise EnvironmentError('Linux IDE selection not implemented. Try opening this folder with CLion.')
        #execute(f'xdg-open', echo=True)

    elif config.android:
        raise EnvironmentError('Android IDE selection not implemented. Try opening this folder with Android Studio.')

def main():
    console(f'========= Mama Build Tool ==========')
    if sys.version_info < (3, 6):
        console('FATAL ERROR: MamaBuild requires Python 3.6')
        exit(-1)

    if len(sys.argv) == 1 or 'help' in sys.argv:
        print_usage()
        sys.exit(-1)

    config = BuildConfig(sys.argv[1:])
    source_dir = os.getcwd()
    name = os.path.basename(source_dir)
    root_dependency = BuildDependency(name, config, BuildTarget, src=source_dir, is_root=True)

    if config.update:
        if not config.target:
            config.target = 'all'
            console(f'Updating all targets')
        else:
            console(f'Updating {config.target} target')

    if config.rebuild:
        config.build = True
        config.clean = True

    if config.clean and not config.target:
        root_dependency.clean()

    load_dependency_chain(root_dependency)
    execute_task_chain(root_dependency)

    if config.open:
        open_project(config, root_dependency)

def __main__():
    main()

if __name__ == '__main__':
    main()
