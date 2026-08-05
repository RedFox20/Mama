#!/usr/bin/python3.10
import sys, os

from .types.local_source import LocalSource
from .utils.system import Color, console, warning, set_run_log
from .utils.sub_process import execute, execute_piped_echo
from .utils.git_status import load_repo_status
from .utils.paths import glob_with_extensions, glob_folders_with_name_match
from .build_config import BuildConfig
from .build_target import BuildTarget
from .build_dependency import BuildDependency
from .dependency_chain import (load_dependency_chain, execute_task_chain, execute_task_chain_parallel,
                               execute_unified, print_sched_debug, find_dependency, get_flat_deps, print_build_banner,
                               get_deps_only_targets, get_deps_that_depend_on_target, DepsOnlyScope,
                               mark_unbuilt_target_deps, sweep_orphaned_build_dirs, load_root, load_display,
                               revive_deferred_target_deps, reload_deferred_deps, load_path_to_target)
from .utils.log_writer import open_run_log
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
    console('                 when combined with a target name, applies to that target\'s dependencies only')
    console('    configure  - force a cmake reconfigure, run configure(), then build')
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
    console('    install-ndk-<ver>   - configures and installs Android NDK <ver> for linux or windows (ex: install-ndk-25c)')
    console('    install-raspi       - installs the Raspberry Pi arm64 cross toolchain (ubuntu/debian)')
    console('    install-raspi32     - installs the legacy Raspberry Pi armv7 cross toolchain')
    console('  args:')
    console('    windows    - build for windows (alias for msvc)')
    console('    msvc       - build for windows using MSVC')
    console('    linux      - build for linux')
    console('    imx8mp     - build for nxp imx8mp yocto')
    console('    xilinx     - build for amd xilinx yocto')
    console('    oclea      - build for oclea/ambarella yocto')
    console('    raspi      - build for raspi (arm64, every Pi since the Pi 3)')
    console('    raspi32    - build for raspi 32-bit armv7 (legacy)')
    console('    mips       - build for mips architecture')
    console('    macos      - build for macos')
    console('    ios        - build for ios')
    console('    android    - build for android')
    console('    android-N  - build for android targeting specific API level, ex: android-26')
    console('    ndk-<ver>  - build for android targeting specific NDK version, ex: ndk-28 or ndk-28.2')
    console('    clang      - prefer clang for linux (default on macos/ios/android)')
    console('    gcc        - prefer gcc for linux')
    console('    fortran    - enable automatic fortran detection (or configure this in mamafile)')
    console('    release    - (default) CMake configuration RelWithDebInfo')
    console('    debug      - CMake configuration Debug')
    console('    arch=x86   - Override cross-compiling architecture: (x86, x64, arm, arm64)')
    console('    x86        - Shorthand for arch=x86, all shorthands: x86 x64 arm arm64')
    console('    jobs=N     - Max number of parallel compilations. (default=system.core.count, minus one on linux)')
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
    console('    globalcache - Keep the cmake compiler seed in the user cache dir, so every checkout shares one probe')
    console('    https-override - rewrite add_git() ssh urls (git@host:path) to https://host/path')
    console('    ssh-override   - rewrite add_git() https urls to ssh (git@host:path)')
    console('    serial     - Disable parallel build of dependencies, useful for debugging')
    console('    buildstats - After the build, print per-package load/configure/build bars; plus a deep')
    console('                 frontend/backend breakdown on MSVC (vcperf) or Clang (-ftime-trace). <target> scopes it')
    console('  examples:')
    console('    mama init                      Initialize a new project. Tries to create mamafile.py and CMakeLists.txt')
    console('    mama build                     Update and build main project only. This only clones, but does not update!')
    console('    mama build x86 opencv          Cross compile build target opencv to x86 architecture')
    console('    mama build android             Cross compile to arm64 android NDK')
    console('    mama build ndk-28              Cross compile with Android NDK 28 (substring match, 28.2 also works)')
    console('    mama build android-26 arm      Cross compile to armv7 android NDK API level 26')
    console('    mama update                    Update all dependencies by doing git pull and build.')
    console('    mama clean                     Cleans main project only.')
    console('    mama clean x86 opencv          Cleans the x86 build of target opencv.')
    console('    mama clean all                 Cleans EVERYTHING in the dependency chain for current arch.')
    console('    mama rebuild                   Cleans, update and build main project only.')
    console('    mama rebuild deps_only         Cleans and rebuilds all dependencies, but not the main project.')
    console('    mama rebuild dep1 deps_only    Cleans and rebuilds only dep1\'s dependencies, skipping dep1 itself.')
    console('    mama configure deps_only       Reconfigures and builds all dependencies, but not the main project.')
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

    # `mama open <shim>` has no source dir to open. Tell the user how to fetch one.
    if found.is_artifactory_shim():
        warning(f'Target {found.name} is an artifactory shim - no source files available locally.')
        console(f'To fetch source, run: mama unshallow {found.name}')
        return

    platform = config.platform
    project = _find_ide_project(platform, found)
    if project:
        execute(f'{platform.ide_open_command} {project}', echo=True)
        return

    if platform.ide_project_ext:
        console(f'Could not find any {" or ".join(platform.ide_project_ext)} projects, using VSCode instead.')
    elif config.linux:
        console(f'Using VSCode. You can also try opening this folder with CLion: {found.src_dir}')
    execute(f'code {found.src_dir}', echo=True)


def _find_ide_project(platform, dep: BuildDependency) -> str:
    """The IDE project this platform's own generator emits, '' when it has none or none was built.
    Visual Studio writes a .sln or .slnx FILE, Xcode a .xcodeproj DIRECTORY.

    The newest match wins. A build dir configured by two toolsets holds both solution formats, and the
    stale one opens an empty solution."""
    exts = platform.ide_project_ext
    if not exts: return ''
    matches = glob_folders_with_name_match(dep.build_dir, exts) \
              if platform.ide_project_is_dir else glob_with_extensions(dep.build_dir, exts)
    return max(matches, key=os.path.getmtime) if matches else ''


_RETIRED_ARGS = {'buildtimes': 'buildstats'}  # removed flags worth naming, so they do not read as a target


def set_target_from_unused_args(config: BuildConfig):
    """An unrecognized bare word is the target name, so `mama rebuild ReCpp` works. An option-shaped arg
    (`jobz=4`, `-foo`) can never be one, so it is a typo - fail here, not 20s later as 'target not found'."""
    for arg in config.unused_args:
        if arg in _RETIRED_ARGS:  # else a removed flag reads as a target and fails with 'target not found'
            console(f"ERROR: '{arg}' was removed, use '{_RETIRED_ARGS[arg]}' instead")
            exit(-1)
        if arg.startswith('-') or '=' in arg:
            console(f"ERROR: unknown option '{arg}'")
            exit(-1)
        if config.has_target():
            console(f"ERROR: Deduced Target='{arg}' from unused argument, but target is already set to '{config.target}'")
            exit(-1)
        else:
            config.target = arg


def _can_unify(config: BuildConfig) -> bool:
    """True for a build/update the unified clone+build scheduler handles: a full tree, or a `deps_only`
    run, which the scheduler scopes to the named target's dependencies as the graph grows. Targeted,
    list, dirty, mama_init and serial runs need the classic load->execute path, which resolves the
    whole tree up front for target lookup and filtering."""
    return (not config.serial_load and (config.build or config.update)
            and (config.no_specific_target() or config.deps_only) and not config.list
            and not config.dirty and not config.mama_init)


def _targeted(config: BuildConfig) -> bool:
    """True when the run names one target, so both the load and the task chain scope to its subtree.
    `all` asks for the whole tree, and `deps_only` scopes itself to the deps of its own target."""
    return config.has_target() and not config.targets_all() and not config.deps_only


def check_config_target(config: BuildConfig, root: BuildDependency, display=None):
    if config.has_target() and not config.targets_all():
        dep = find_dependency(root, config.target)
        # The target may hide below a deferred dep. A cached package expands first, because it costs
        # no network. The deps that need a fetch or a clone expand only when the name is still missing.
        for free_only in (True, False):
            if dep is None and reload_deferred_deps(root, free_only, display):
                dep = find_dependency(root, config.target)
        if dep is None:  # list what IS valid: the name is likely a misspelled target or flag
            names = ', '.join(sorted(d.name for d in get_flat_deps(root)))
            console(f"ERROR: specified target='{config.target}' not found! Available targets: {names}")
            exit(-1)


def print_package_exports(dep: BuildDependency):
    target:BuildTarget = dep.target
    if dep.from_artifactory or target.try_automatic_artifactory_fetch():
        archive = f' {dep.artifactory_archive}' if dep.artifactory_archive else ''
        console(f'    Target {target.name} fetched from artifactory{archive}')
    else:
        console(f'    Target {target.name} local build at {target.build_dir()}')
    target.print_exports(abs_paths=True)


def mama_dirty(root: BuildDependency, dep: BuildDependency):
    """ Mark `dep` dirty, and also every project that depends on `dep` """
    dirty_chain = get_deps_that_depend_on_target(root, dep)
    if root.config.print:
        used_by = ", ".join([d.name for d in dirty_chain]) if dirty_chain else 'none'
        console(f'    Target {dep.name} used by: {used_by}')
    dep.dirty()
    for d in dirty_chain:
        d.dirty()


def run_coverage_report(target: BuildTarget):
    if not target.config.platform.supports_coverage_report:
        console(f'Coverage report not supported yet on {target.config.name()}')
        return
    root = target.source_dir(target.config.coverage_report)
    gcov_exec = ''
    if target.config.gcc and target.config.cc_path:
        # Derive gcov path from gcc path: e.g. /usr/bin/gcc-14 -> /usr/bin/gcov-14
        gcov_path = os.path.realpath(target.config.cc_path).replace('gcc', 'gcov')
        if os.path.exists(gcov_path):
            gcov_exec = f'--gcov-executable "{gcov_path}" '
    cmd = 'gcovr --gcov-ignore-errors all --gcov-ignore-parse-errors all ' \
        + '--sort uncovered-percent ' \
        + gcov_exec \
        + f'--root "{root}" "{target.build_dir()}"'
    try:
        # a report failure must not break CI, so log the error instead of an exit.
        # CI checks stdout for the report result separately.
        status, _ = execute_piped_echo(cwd=target.source_dir(), cmd=cmd, echo=True)
        if status != 0:
            warning(f'WARNING: gcovr exited {status} - coverage report may be incomplete')
    except Exception as e:
        console(f'ERROR: Coverage report failed: {e}', color=Color.RED)


def mamabuild(args, source_dir=os.getcwd()):
    """Main entry point for MamaBuild. Parses the command line arguments and executes the requested actions.
    - args: list of command line arguments, without the script name, e.g. ['build', 'target=all', 'debug']
    - source_dir: the directory to treat as the main project source
    """
    if sys.version_info < (3, 10):
        console('FATAL ERROR: MamaBuild requires Python 3.10 or higher')
        exit(-1)

    if len(args) == 0 or 'help' in args or '--help' in args:
        print_title()
        print_usage()
        exit(-1)
    if 'version' in args or '--version' in args:
        console(f'MamaBuild version {__version__}')
        exit(0)

    config = BuildConfig(args)
    config.root_source_dir = source_dir  # cwd for any `mama <host> build` bootstrap child (build_host_binary)
    if config.print:
        if config.verbose:
            console(f'Build jobs={config.jobs}')

    name = os.path.basename(source_dir)
    local_src = LocalSource(name, source_dir, mamafile=None, always_build=False, args=[])
    workspace = None # the root mamafile.py decides the workspace later
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
        if config.no_specific_target():
            config.target = 'all'
            if config.print: console(f'Updating all targets')
        else:
            if config.print: console(f'Updating {config.target} target')

    deps_only_target_name = None
    if config.deps_only:
        if config.no_specific_target():
            config.target = 'all'
            if config.print: console(f'Executing deps_only action on all targets')
        else:
            deps_only_target_name = config.target
            if config.print: console(f'Executing deps_only action on {deps_only_target_name} target dependencies')

    if config.rebuild:
        config.build = True
        config.clean = True

    if config.clean and config.no_target() and not config.deps_only:
        root.clean()

    # ONE `git status` for the whole run, before the walk, so every local dependency reads its own
    # subfolder out of it instead of spawning its own git. Eager, so a parallel load needs no lock.
    load_repo_status(source_dir)

    # The root loads before every other dep: its settings() locks the compiler that names each dep dir,
    # and its mamafile names the workspace. The run then owns ONE build log, which every phase writes to.
    load_root(root)
    set_run_log(open_run_log(config.workspaces_root, root.workspace))

    if config.sched_debug:  # TEMP: load the tree, print the build-weight calc per target, then stop
        load_dependency_chain(root)
        print_sched_debug(root)
        return

    # Full build/update and deps_only -> unified clone+configure+build scheduler. Everything else
    # needs the fully loaded tree up front for lookup/filtering -> classic load->execute path.
    if _can_unify(config):
        execute_unified(root, DepsOnlyScope(config, deps_only_target_name) if config.deps_only else None)
        dep = root
        flat_deps = get_flat_deps(root)  # the graph is fully grown by now, keep this defined for the code below
    else:
        # One live region for the whole load, so parallel clones report on one line each. Stage two runs
        # inside it, and it closes before the package listing, which prints as plain lines.
        with load_display(config) as display:
            # `dirty` marks every dependent of the target, and only a full load names them all
            if _targeted(config) and not config.dirty:
                load_path_to_target(root)
            else:
                load_dependency_chain(root, display)
            check_config_target(config, root, display)

            # Stage two: load the subtree of the target, which stage one stopped short of. It runs BEFORE
            # the clean_only return below, because a clean acts inside the load of the target it names.
            if _targeted(config):
                revive_deferred_target_deps(root, config, display)

        # clean is not a build: the load wiped the dirs, so a packaging pass fabricates an empty package
        # or fails a mamafile assert ('libX.so not found'). rebuild sets build=True, so it still runs.
        if config.clean_only():
            if config.targets_all(): sweep_orphaned_build_dirs(root, config)  # deps with no source on disk
            return

        # Only now is the tree loaded, so X's subtree is known: revive the deps X needs but that have
        # nothing on disk. _should_build cannot do this at load time - deps have no parent link then.
        if _targeted(config) and (config.build or config.update):
            mark_unbuilt_target_deps(root, config)

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

        # EVERY action scopes to its target. An out-of-scope dep builds nothing, yet it still reaches
        # _run_packaging, where a mamafile asserts on libs that no run produced.
        if _targeted(config) and dep is not None:
            flat_deps = get_flat_deps(dep)
            flat_deps_reverse = list(reversed(flat_deps))

        if config.deps_only:
            if deps_only_target_name:
                flat_deps, flat_deps_reverse = get_deps_only_targets(root, deps_only_target_name, config)
                config.target = 'all'
            else:
                flat_deps.remove(root)
                flat_deps_reverse.remove(root)

        if config.list:
            # a list run builds nothing, so mark every dep no-build
            for d in flat_deps:
                d.target.nothing_to_build()

        if config.dirty:
            if not dep:
                console(f'dirty command failed: target {config.target} not found')
                exit(-1)
            mama_dirty(root, dep)
            return

        if config.build or config.update:  # not for list/deploy/test runs, which build nothing
            print_build_banner(config, len(flat_deps_reverse))

        if config.verbose:
            chain = ' -> '.join([d.name for d in flat_deps_reverse])
            console(f'Executing task chain for build:\n    {chain}', Color.BLUE)

        # Parallel by default, only an explicit `serial` selects the serial runner. Even a one-dep graph
        # goes parallel: that scheduler owns the live display, and the serial runner dumps raw cmake output.
        if config.serial_load:
            execute_task_chain(flat_deps_reverse)
        else:
            execute_task_chain_parallel(flat_deps_reverse)

    if config.list:
        flat_deps_names = [d.name for d in flat_deps]
        if config.no_specific_target():
            console(f'    ALL Dependency List: {flat_deps_names}', Color.BLUE)
            for d in flat_deps: print_package_exports(d)
        else:
            console(f'    {dep.name} Dependency List: {flat_deps_names}', Color.BLUE)
            print_package_exports(dep)
    elif config.verbose:
        print_package_exports(dep if dep else root)

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

