#!/usr/bin/python3.10
import sys, os

from .types.local_source import LocalSource
from .utils.system import Color, console
from .utils.sub_process import execute
from .util import glob_with_extensions, glob_folders_with_name_match
from .build_config import BuildConfig
from .build_target import BuildTarget
from .build_dependency import BuildDependency
from .dependency_chain import load_dependency_chain, execute_task_chain, find_dependency, get_flat_deps, get_deps_that_depend_on_target
from .init_project import mama_init_project
from ._version import __version__

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
    console('    serve      - Equivalent of `update rebuild deploy upload`')
    console('    clean      - clean main project or specific target')
    console('    rebuild    - clean, update and build main project or specific target')
    console('    wipe       - wipe specific target dependency and clone it again')
    console('    reclone    - (deprecated) alias for wipe')
    console('    dirty      - mark a target for rebuild even if it was up to date')
    console('    deps_only  - only execute build/rebuild/clean on dependencies, skip the main target')
    console('    configure  - run configure() task on target dependencies without rebuilding')
    console('    upload     - uploads target package to artifactory server')
    console('    if_needed  - only uploads if package does not exist on server')
    console('    art        - always fetch pkgs from artifactory, failure will throw an error')
    console('    noart      - temporarily ignore artifactory pkgs fetch')
    console('    test       - run tests for main project or specific target')
    console('    start=arg  - start a specific tool via mamafile.start(args)')
    console('    open=<tgt> - open a project file')
    console('    version    - shows this package version and exits')
    console('    help       - shows this help list')
    console('  install utils:')
    console('    install-clang-<ver> - configures and installs clang-<ver> for ubuntu, ex: install-clang-18')
    console('    install-gcc-<ver>   - configures and installs gcc-<ver> for ubuntu, ex: install-gcc-13')
    console('    install-msbuild     - configures and installs MSBuild for linux')
    console('    install-ndk-<ver>   - configures and installs Android NDK <ver> for linux or windows (ex: install-ndk-25b)')
    console('  args:')
    console('    windows    - build for windows (alias for msvc)')
    console('    msvc       - build for windows using MSVC')
    console('    linux      - build for linux')
    console('    imx8mp     - build for nxp imx8mp yocto')
    console('    xilinx     - build for amd xilinx yocto')
    console('    oclea      - build for oclea/ambarella yocto')
    console('    raspi      - build for raspi')
    console('    mips       - build for mips architecture')
    console('    macos      - build for macos')
    console('    ios        - build for ios')
    console('    android    - build for android')
    console('    android-N  - build for android targeting specific API level, ex: android-26')
    console('    ndk-<ver>  - build for android targeting specific NDK version, ex: ndk-28 or ndk-28.2')
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
    console('    with_tests - Forces CMake option -DENABLE_TESTS=ON and -DBUILD_TESTS=ON')
    console('    test_until_failure - Runs tests in a loop until they fail, defaults to N=100, useful to catch flaky tests')
    console('    test_until_failure=N - Runs tests in a loop until they fail, with a maximum of N iterations')
    console('    sanitize=  - enables -fsanitize= for gcc/clang builds [address|leak|thread|undefined]')
    console('    asan|lsan|tsan|ubsan - shorthands for sanitize=address|leak|thread|undefined respectively')
    console('    clang-tidy - enables clang-tidy static analysis during build, clang-tidy must be in PATH')
    console('    coverage   - Builds the project with GCC --coverage option')
    console('    coverage-report[=src_root] - Generates coverage report using gcovr')
    console('    silent     - Greatly reduces verbosity')
    console('    verbose    - Greatly increases verbosity for build dependencies and cmake')
    console('    parallel   - Load dependencies in parallel')
    console('    unshallow  - Allow unshallowing shallow git clones')
    console('  examples:')
    console('    mama init                      Initialize a new project. Tries to create mamafile.py and CMakeLists.txt')
    console('    mama build                     Update and build main project only. This only clones, but does not update!')
    console('    mama build x86 opencv          Cross compile build target opencv to x86 architecture')
    console('    mama build android             Cross compile to arm64 android NDK')
    console('    mama build ndk-28              Cross compile specifically with Android NDK 28 (substring match, so 28.2 will also work)')
    console('    mama build android-26 arm      Cross compile to armv7 android NDK API level 26')
    console('    mama update                    Update all dependencies by doing git pull and build.')
    console('    mama clean                     Cleans main project only.')
    console('    mama clean x86 opencv          Cleans main project only.')
    console('    mama clean all                 Cleans EVERYTHING in the dependency chain for current arch.')
    console('    mama rebuild                   Cleans, update and build main project only.')
    console('    mama rebuild deps_only         Cleans and rebuilds all dependencies, but not the main project.')
    console('    mama configure deps_only       Re-runs CMake configure on all dependencies, but not the main project.')
    console('    mama build dep1                Update and build dep1 only.')
    console('    mama update dep1               Update and build the specified target.')
    console('    mama serve android             Update, build and deploy for Android')
    console('    mama wipe dep1                 Wipe target dependency completely and clone again. Does not build!')
    console('    mama upload dep1               Deploys and uploads dependency to Artifactory server.')
    console('    mama test                      Run tests on main project.')
    console('    mama test=arg                  Run tests on main project with an argument.')
    console('    mama test="arg1 arg2"          Run tests on main project with multiple arguments.')
    console('    mama test dep1                 Run tests on target dependency project.')
    console('    mama start=dbtool              Call main project mamafile start() with args [`dbtool`].')
    console('    mama rebuild all tsan lsan     Rebuild all targets with thread and leak sanitizers enabled.')
    console('    mama rebuild all sanitize=leak,undefined Rebuild all targets with leak and undefined sanitizers enabled.')
    console('  environment:')
    console('    setenv("NINJA")                  Path to NINJA build executable')
    console('    setenv("ANDROID_HOME")           Path to Android SDK if auto-detect fails')
    console('    setenv("MAMA_ARTIFACTORY_USER")  Username for Artifactory server')
    console('    setenv("MAMA_ARTIFACTORY_PASS")  Password for Artifactory server')


def open_project(config: BuildConfig, root_dependency: BuildDependency):
    name = config.target if config.has_target() and not config.targets_all() else config.open
    found = root_dependency if name == 'root' else find_dependency(root_dependency, name)
    if not found:
        raise KeyError(f'No project named {name}')
    
    if config.msvc:
        solutions = glob_with_extensions(found.build_dir, ['.sln'])
        if solutions:
            execute(f'start {solutions[0]}', echo=True)
        else:
            console('Could not find any Visual Studio solutions, using VSCode instead.')
            execute(f'code {found.src_dir}', echo=True)

    elif config.macos or config.ios:
        projects = glob_folders_with_name_match(found.build_dir, ['.xcodeproj'])
        if projects:
            execute(f'open {projects[0]}', echo=True)
        else:
            console('Could not find any Xcode projects, using VSCode instead.')
            execute(f'code {found.src_dir}', echo=True)

    elif config.linux:
        console(f'Using VSCode. You can also try opening this folder with CLion: {found.src_dir}')
        execute(f'code {found.src_dir}', echo=True)
        #execute(f'xdg-open', echo=True)

    elif config.android:
        console('Android IDE selection not implemented, using VSCode instead.')
        execute(f'code {found.src_dir}', echo=True)

def set_target_from_unused_args(config: BuildConfig):
    for arg in config.unused_args:
        if config.has_target():
            console(f"ERROR: Deduced Target='{arg}' from unused argument, but target is already set to '{config.target}'")
            exit(-1)
        else:
            config.target = arg

def check_config_target(config: BuildConfig, root: BuildDependency):
    if config.has_target() and not config.targets_all():
        dep = find_dependency(root, config.target)
        if dep is None:
            console(f"ERROR: specified target='{config.target}' not found!")
            exit(-1)


def print_package_exports(dep: BuildDependency):
    target:BuildTarget = dep.target
    if dep.from_artifactory or target.try_automatic_artifactory_fetch():
        console(f'    Target {target.name} fetched from artifactory')
    else:
        target.package()
    target.print_exports(abs_paths=True)


def mama_dirty(root: BuildDependency, dep: BuildDependency):
    """ Marks `dep` as dirty and also marks all projects that depend on `dep` as dirty """
    dirty_chain = get_deps_that_depend_on_target(root, dep)
    if root.config.print:
        used_by = ", ".join([d.name for d in dirty_chain]) if dirty_chain else 'none'
        console(f'    Target {dep.name} used by: {used_by}')
    dep.dirty()
    for d in dirty_chain:
        d.dirty()


def run_coverage_report(target: BuildTarget):
    if target.config.msvc:
        console('Coverage report not supported yet on Windows')
        return
    root = target.source_dir(target.config.coverage_report)
    gcov_exec = ''
    if target.config.gcc and target.config.cc_path:
        # Derive gcov path from gcc path: e.g. /usr/bin/gcc-14 -> /usr/bin/gcov-14
        gcov_path = os.path.realpath(target.config.cc_path).replace('gcc', 'gcov')
        if os.path.exists(gcov_path):
            gcov_exec = f'--gcov-executable "{gcov_path}" '
    # this is too verbose for CI
    #verbose = '--verbose ' if target.config.verbose else ''
    cmd = 'gcovr --gcov-ignore-parse-errors negative_hits.warn ' \
        + '--sort uncovered-percent ' \
        + gcov_exec \
        + f'--root "{root}" "{target.build_dir()}"'
    try:
        # throw if coverage fails, but don't exit with error, so we don't break CI on coverage report failures
        # instead stdout must be checked for coverage report success or failure separately
        target.run(cmd, src_dir=True, exit_on_fail=False)
    except Exception as e:
        console(f'ERROR: Coverage report failed: {e}', color=Color.RED)


def mamabuild(args, source_dir=os.getcwd()):
    """ Main entry point for MamaBuild. Parses command line arguments and executes the requested actions. 
        - args: list of command line arguments, without the script name. Ex: ['build', 'target=all', 'debug']
        - source_dir: the directory to treat as the main project source
    """
    if sys.version_info < (3, 10):
        console('FATAL ERROR: MamaBuild requires Python 3.10 or higher')
        exit(-1)

    if len(args) == 0 or 'help' in args:
        print_title()
        print_usage()
        exit(-1)
    if 'version' in args:
        console(f'MamaBuild version {__version__}')
        exit(0)

    config = BuildConfig(args)
    if config.print:
        print_title()
        if config.verbose:
            console(f'Build jobs={config.jobs}')

    name = os.path.basename(source_dir)
    local_src = LocalSource(name, source_dir, mamafile=None, always_build=False, args=[])
    workspace = None # figure out the workspace from the root mamafile.py
    root = BuildDependency(None, config, workspace, local_src)

    if config.unused_args:
        set_target_from_unused_args(config)

    # root init
    if config.mama_init and config.no_target():
        mama_init_project(root)
        return

    if config.convenient_install:
        config.run_convenient_installs()
        return

    has_cmake = root.cmakelists_exists()
    if not root.mamafile_exists() and not has_cmake:
        console('FATAL ERROR: mamafile.py not found and CMakeLists.txt not found')
        exit(-1)

    if config.update:
        if config.no_target():
            config.target = 'all'
            if config.print: console(f'Updating all targets')
        else:
            if config.print: console(f'Updating {config.target} target')

    if config.deps_only and config.no_target():
        config.target = 'all'

    if config.rebuild:
        config.build = True
        config.clean = True

    if config.clean and config.no_target() and not config.deps_only:
        root.clean()

    load_dependency_chain(root)
    check_config_target(config, root)

    # get the main target dependency
    if config.has_target():
        dep = find_dependency(root, config.target)
    else:
        dep = root

    # target init
    if config.mama_init and config.has_target():
        if not dep:
            console(f'init command failed: target {config.target} not found')
            exit(-1)
        mama_init_project(dep)
        return

    flat_deps = get_flat_deps(root) # root, dep2, deepest_dep
    flat_deps_reverse = list(reversed(flat_deps)) # deepest_dep, dep2, root

    if config.deps_only:
        flat_deps.remove(root)
        flat_deps_reverse.remove(root)

    if config.list:
        flat_deps_names = [d.name for d in flat_deps]
        if config.no_specific_target():
            console(f'    ALL Dependency List: {flat_deps_names}', Color.BLUE)
            for d in flat_deps: print_package_exports(d)
        else:
            console(f'    {dep.name} Dependency List: {flat_deps_names}', Color.BLUE)
            print_package_exports(dep)
        return

    if config.dirty:
        if not dep:
            console(f'dirty command failed: target {config.target} not found')
            exit(-1)
        mama_dirty(root, dep)
        return

    # initialize platform compiler config
    if config.android: config.android.android_home()
    if config.raspi:   config.raspi_bin()
    if config.yocto_linux: config.yocto_linux.init_default()
    if config.mips:        config.mips.init_default()

    if config.verbose:
        chain = ' -> '.join([d.name for d in flat_deps_reverse])
        console(f'Executing task chain for build:\n    {chain}', Color.BLUE)
        print_package_exports(root)

    execute_task_chain(flat_deps_reverse)

    if config.coverage_report:
        if not dep:
            console(f'coverage-report failed: target {config.target} not found')
            exit(-1)
        run_coverage_report(dep.target)
        return

    if dep and config.test and dep.get_enabled_coverage():
        console('Project was built with coverage, generating coverage report')
        run_coverage_report(dep.target)
        return

    if config.open:
        open_project(config, root)


def main(): # for backwards compat with v0.10.x
    mamabuild(sys.argv[1:])


def __main__():
    mamabuild(sys.argv[1:])


if __name__ == '__main__':
    mamabuild(sys.argv[1:])

