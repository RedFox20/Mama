#!/usr/bin/python3.6
import sys, os, importlib
from .system import console, execute
from .util import glob_with_extensions, glob_folders_with_name_match
from .build_config import BuildConfig
from .build_target import BuildTarget
from .build_dependency import BuildDependency
from .dependency_chain import load_dependency_chain, execute_task_chain, find_dependency, get_full_flattened_deps
from .init_project import mama_init_project

def print_title():
    console(f'========= Mama Build Tool ==========')

def print_usage():
    console('mama [actions...] [args...]')
    console('  actions:')
    console('    init       - create initial mamafile.py and CMakeLists.txt')
    console('    list       - list all mama dependencies on this project')
    console('    build      - configure and build main project or specific target, this can clone, but does not pull')
    console('    update     - update and build target dependencies after calling git pull')
    console('    deploy     - runs PAPA deploy stage by gathering all libraries and assets')
    console('    serve      - Equivalent of `update build deploy`')
    console('    clean      - clean main project or specific target')
    console('    rebuild    - clean, update and build main project or specific target')
    console('    reclone    - wipe specific target dependency and clone it again')
    console('    wipe       - alias of reclone')
    console('    test       - run tests for main project or specific target')
    console('    start=arg  - start a specific tool via mamafile.start(args)')
    console('    add        - add new dependency')
    console('    new        - create new mama build file')
    console('    open=<tgt> - open a project file')
    console('    help       - shows this help list')
    console('  install utils:')
    console('    install-clang6  - configures and installs clang6 for linux')
    console('    install-msbuild - configures and installs MSBuild for linux')
    console('  args:')
    console('    windows    - build for windows')
    console('    linux      - build for linux')
    console('    macos      - build for macos')
    console('    ios        - build for ios')
    console('    android    - build for android')
    console('    android-N  - build for android targeting specific API level, ex: android-26')
    console('    clang      - prefer clang for linux (default on linux/macos/ios/android)')
    console('    gcc        - prefer gcc for linux')
    console('    fortran    - enable automatic fortran detection (or configure this in mamafile)')
    console('    release    - (default) CMake configuration RelWithDebInfo')
    console('    debug      - CMake configuration Debug')
    console('    arch=x86   - Override cross-compiling architecture: (x86, x64, arm, arm64)')
    console('    x86        - Shorthand for arch=x86, all shorthands: x86 x64 arm arm64')
    console('    jobs=N     - Max number of parallel compilations. (default=system.core.count)')
    console('    target=P   - Name of the target')
    console('    all        - Short for target=all')
    console('    silent     - Greatly reduces verbosity')
    console('    verbose    - Greatly increases verbosity for build dependencies and cmake')
    console('  examples:')
    console('    mama init                      Initialize a new project. Tries to create mamafile.py and CMakeLists.txt')
    console('    mama build                     Update and build main project only. This only clones, but does not update!')
    console('    mama build x86 opencv          Cross compile build target opencv to x86 architecture')
    console('    mama build android             Cross compile to arm64 android NDK')
    console('    mama build android-26 arm      Cross compile to armv7 android NDK API level 26')
    console('    mama update                    Update all dependencies by doing git pull and build.')
    console('    mama clean                     Cleans main project only.')
    console('    mama clean x86 opencv          Cleans main project only.')
    console('    mama clean all                 Cleans EVERYTHING in the dependency chain for current arch.')
    console('    mama rebuild                   Cleans, update and build main project only.')
    console('    mama build dep1                Update and build dep1 only.')
    console('    mama update dep1               Update and build the specified target.')
    console('    mama serve android             Update, build and deploy for Android')
    console('    mama wipe dep1                 Wipe target dependency completely and clone again.')
    console('    mama test                      Run tests on main project.')
    console('    mama test=arg                  Run tests on main project with an argument.')
    console('    mama test="arg1 arg2"          Run tests on main project with multiple arguments.')
    console('    mama test dep1                 Run tests on target dependency project.')
    console('    mama start=dbtool              Call main project mamafile start() with args [`dbtool`].')
    console('  environment:')
    console('    setenv("NINJA")                Path to NINJA build executable')
    console('    setenv("ANDROID_HOME")         Path to Android SDK if auto-detect fails')


def open_project(config: BuildConfig, root_dependency: BuildDependency):
    name = config.target if config.target and config.target != 'all' else config.open
    found = root_dependency if name == 'root' else find_dependency(root_dependency, name)
    if not found:
        raise KeyError(f'No project named {name}')
    
    if config.windows:
        solutions = glob_with_extensions(found.build_dir, ['.sln'])
        if not solutions:
            raise EnvironmentError('Could not find any Visual Studio solutions!')
        execute(f'start {solutions[0]}', echo=True)

    elif config.macos or config.ios:
        projects = glob_folders_with_name_match(found.build_dir, ['.xcodeproj'])
        if not projects:
            raise EnvironmentError('Could not find any Xcode projects!')
        execute(f'open {projects[0]}', echo=True)

    elif config.linux:
        raise EnvironmentError('Linux IDE selection not implemented. Try opening this folder with CLion.')
        #execute(f'xdg-open', echo=True)

    elif config.android:
        raise EnvironmentError('Android IDE selection not implemented. Try opening this folder with Android Studio.')

def set_target_from_unused_args(config: BuildConfig):
    for arg in config.unused_args:
        if config.target:
            console(f"ERROR: Deduced Target='{arg}' from unused argument, but target is already set to '{config.target}'")
            exit(-1)
        else:
            config.target = arg

def check_config_target(config: BuildConfig, root: BuildDependency):
    if config.target and config.target != 'all':
        dep = find_dependency(root, config.target)
        if dep is None:
            console(f"ERROR: specified target='{config.target}' not found!")
            exit(-1)

def main():
    if sys.version_info < (3, 6):
        console('FATAL ERROR: MamaBuild requires Python 3.6')
        exit(-1)

    if len(sys.argv) == 1 or 'help' in sys.argv:
        print_title()
        print_usage()
        exit(-1)

    config = BuildConfig(sys.argv[1:])
    if config.print:
        print_title()

    source_dir = os.getcwd()
    name = os.path.basename(source_dir)
    root = BuildDependency(name, config, BuildTarget, src=source_dir, is_root=True)

    if config.mama_init:
        mama_init_project(root)
        return

    if config.convenient_install:
        config.run_convenient_installs()
        return

    has_cmake = root.cmakelists_exists()
    if not root.mamafile_exists() and not has_cmake:
        console('FATAL ERROR: mamafile.py not found and CMakeLists.txt not found')
        exit(-1)

    if config.unused_args:
        set_target_from_unused_args(config)

    if config.update:
        if not config.target:
            config.target = 'all'
            if config.print: console(f'Updating all targets')
        else:
            if config.print: console(f'Updating {config.target} target')

    if config.rebuild:
        config.build = True
        config.clean = True

    if config.clean and not config.target:
        root.clean()

    load_dependency_chain(root)
    if config.list:
        print(f'Dependency List: {get_full_flattened_deps(root)}')
        return

    check_config_target(config, root)

    if config.android: config.init_ndk_path()
    if config.raspi:   config.init_raspi_path()

    execute_task_chain(root)

    if config.open:
        open_project(config, root)


def __main__():
    main()


if __name__ == '__main__':
    main()

