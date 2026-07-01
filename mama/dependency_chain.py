import os, sys, time, concurrent.futures
from typing import List

from mama.build_config import BuildConfig
from .build_dependency import BuildDependency
from .util import read_text_from, write_text_to, save_file_if_contents_changed, get_time_str
from .utils import ssh_multiplex, system
from .utils.sub_process import SubProcess
from .utils.system import Color, console, error, warning, get_colored_text


def _get_cmake_path_list(paths):
    return ''.join(f'\n    "{path}"' for path in paths)


def _get_exported_libs(target):
    filtered = []
    allowed = []
    is_linux_like = False
    if target.android or target.linux or target.raspi or target.mips or target.yocto_linux:
        is_linux_like = True
    elif target.msvc:
        allowed = ['.lib']
    elif target.macos:
        allowed = ['.a', '.dylib', '.bundle']
    elif target.ios:
        allowed = ['.a', '.dylib', '.framework']

    #print(f'{target.name: <16} exported: {target.exported_libs}')
    for lib in target.exported_libs:
        if not lib: continue
        if is_linux_like:
            # for linux-like targets allow .a, .so and versioned .so.1.2.3 files
            if lib.endswith('.a') or lib.endswith('.so') \
                or (str.isdigit(lib[-1]) and '.so.' in lib):
                filtered.append(lib)
        else:
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


def get_flat_deps(root: BuildDependency):
    """ Gets flat dependencies, including root """
    return [root] + _get_flattened_deps(root)


def get_flat_child_deps(dep: BuildDependency):
    """ Gets flat child dependencies of dep, excluding dep itself """
    return _get_flattened_deps(dep)


def get_deps_only_targets(root: BuildDependency, deps_only_target_name: str, config: BuildConfig):
    """
    For `deps_only` with a specific target, returns (flat_deps, flat_deps_reverse)
    containing only the dependencies of the named target.
    Also marks those deps for rebuild and cleans them if needed.
    """
    deps_only_dep = find_dependency(root, deps_only_target_name)
    flat_deps = _get_flattened_deps(deps_only_dep)
    flat_deps_reverse = list(reversed(flat_deps))
    if config.build or config.update:
        for d in flat_deps_reverse:
            if config.clean:
                d.clean()
                d.create_build_dir_if_needed()
            d.should_rebuild = True
    return flat_deps, flat_deps_reverse


def get_deps_that_depend_on_target(root: BuildDependency, target: BuildDependency, deps = []) -> List[BuildDependency]:
    discovered_new = False
    """ Gets all dependencies that depend on the target """
    def depth_first_search_for_target(dep: BuildDependency):
        nonlocal discovered_new, target, deps
        depends = False
        for child in dep.get_children():
            if child in deps:
                continue
            if child == target:
                depends = True
            if depth_first_search_for_target(child):
                deps.append(child)
                depends = True
                discovered_new = True
        return depends
    if depth_first_search_for_target(root) and root not in deps:
        deps.append(root)
        discovered_new = True

    # now that we have the initial deps,
    # we need to further expand it to include second level dependencies
    while discovered_new:
        discovered_new = False
        for d in deps:
            depth_first_search_for_target(d)
            if discovered_new:
                break # restart the outer loop
    return deps


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


def _get_compile_commands_path(dep: BuildDependency):
    src_build_cmds = f'{dep.src_dir}/build/compile_commands.json'
    bin_build_cmds = f'{dep.build_dir}/compile_commands.json'

    src_exists = os.path.exists(src_build_cmds)
    bin_exists = os.path.exists(bin_build_cmds)

    # choose the latest one
    if src_exists and bin_exists and os.path.getmtime(src_build_cmds) > os.path.getmtime(bin_build_cmds):
        # for src_dir paths we use `${workspaceFolder}` macro:
        return '${workspaceFolder}/build/compile_commands.json'
    if bin_exists:
        # for build dir paths, check if build dir is relative to src dir
        if dep.build_dir.startswith(dep.src_dir):
            # if so, we chop off the src dir and use `${workspaceFolder}/`
            rel_build_dir = f'${{workspaceFolder}}{dep.build_dir[len(dep.src_dir):]}/compile_commands.json'
            return rel_build_dir
        return bin_build_cmds # absolute path for build dir paths
    return None


def _find_matching_platform_config(dep: BuildDependency, configurations):
    config_name = dep.config.name()
    config_arch = dep.config.arch

    # first look for Platform + Arch match such as Windows x64
    for conf in configurations:
        name = str(conf["name"]).lower()
        if config_name in name and config_arch in name:
            return conf

    # then look for only Platform match like 'windows'
    for conf in configurations:
        name = str(conf["name"]).lower()
        if config_name in name:
            return conf
    return None


def _save_vscode_compile_commands(dep: BuildDependency):
    if not dep.src_dir: # for artifactory pkgs, there is no src_dir
        return
    if not dep.is_root:
        return
    # ASAN/TSAN/coverage are temporary diagnostic builds living in a suffixed build dir
    # (eg linux-asan). Don't repoint the IDE away from the canonical build on every such run.
    if dep.config.sanitize or dep.config.coverage:
        return

    cpp_props_path = f'{dep.src_dir}/.vscode/c_cpp_properties.json'
    if not os.path.exists(cpp_props_path):
        return

    commands_path = _get_compile_commands_path(dep)
    if not commands_path:
        return

    # we have a valid path for compile_commands.json, now link it into c_cpp_properties.json
    cpp_props_text = read_text_from(cpp_props_path)
    import json
    props = json.loads(cpp_props_text)
    configurations = props["configurations"]

    platform_config = _find_matching_platform_config(dep, configurations)

    # make a copy of the first config and rename it to the platform name
    if not platform_config and len(configurations) > 0:
        platform_config = configurations[0].copy()
        platform_config['name'] = f'{dep.config.name()} {dep.config.arch}'
        configurations.append(platform_config)

    # set the compile commands for this platform
    if platform_config:
        platform_config["compileCommands"] = commands_path

    new_cpp_props_text = json.dumps(props, indent=4)
    if new_cpp_props_text != cpp_props_text:
        write_text_to(cpp_props_path, new_cpp_props_text)
        if dep.config.print and platform_config:
            console(f'Updated c_cpp_properties.json "{platform_config["name"]}" compileCommands')


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
    includes_def, package_text = _get_dependency_cmake_defines(root)
    includes_defs = [includes_def]
    text += package_text

    root.flattened_deps = _get_flattened_deps(root)
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
    c:BuildConfig = root.config

    def get_build_dir_defines(build_dir):
        build_dir = c.build_dir_with_suffix(build_dir)
        return f'''set(MAMA_BUILD "{build_dir}")
        {_get_mama_dependencies_cmake(root, build_dir)}'''

    text = f'''# This file is auto-generated by mama build. Do not modify by hand!
if(CMAKE_CXX_COMPILER_ID MATCHES "Clang")
    set(CLANG TRUE)
elseif(CMAKE_CXX_COMPILER_ID MATCHES "GNU")
    set(GCC TRUE)
endif()

if(CMAKE_GENERATOR_PLATFORM)
    set(MAMA_CMAKE_ARCH ${{CMAKE_GENERATOR_PLATFORM}})
elseif(ANDROID OR ANDROID_NDK)
    set(MAMA_CMAKE_ARCH ${{ANDROID_ARCH}})
elseif(CMAKE_SYSTEM_PROCESSOR)
    set(MAMA_CMAKE_ARCH ${{CMAKE_SYSTEM_PROCESSOR}})
else()
    message(FATAL_ERROR "MAMA: Missing CMake target architecture!")
endif()

# Initializes the INCLUDE and LIBS, they will overwritten in mama-dependencies.cmake
set(MAMA_INCLUDE "")
set(MAMA_LIBS "")

# Set MAMA_INCLUDES and MAMA_LIBS for each platform
if(ANDROID OR ANDROID_NDK)
    if(MAMA_CMAKE_ARCH MATCHES "(arm64)|(ARM64)")
        set(MAMA_ARCH_ARM64 TRUE)
        {get_build_dir_defines(c.build_dir_android64())}
    else()
        set(MAMA_ARCH_ARM32 TRUE)
        {get_build_dir_defines(c.build_dir_android32())}
    endif()
elseif(WIN32)
    if(MAMA_CMAKE_ARCH MATCHES "(amd64)|(AMD64)|(IA64)|(x64)|(X64)|(x86_64)|(X86_64)")
        set(MAMA_ARCH_X64 TRUE)
        {get_build_dir_defines(c.build_dir_win64())}
    elseif(MAMA_CMAKE_ARCH MATCHES "(X86)|(x86)|(i386)|(i686)")
        set(MAMA_ARCH_X86 TRUE)
        {get_build_dir_defines(c.build_dir_win32())}
    elseif(MAMA_CMAKE_ARCH MATCHES "ARM64")
        set(MAMA_ARCH_ARM64 TRUE)
        {get_build_dir_defines(c.build_dir_winarm64())}
    elseif(MAMA_CMAKE_ARCH MATCHES "ARM")
        set(MAMA_ARCH_ARM32 TRUE)
        {get_build_dir_defines(c.build_dir_winarm32())}
    else()
        message(FATAL_ERROR "MAMA: Unrecognized target architecture '${{MAMA_CMAKE_ARCH}}'")
    endif()
elseif(APPLE)
  if(IOS_PLATFORM)
        set(IOS TRUE)
        set(MAMA_ARCH_ARM64 TRUE) # Always arm64
        {get_build_dir_defines(c.build_dir_ios())}
  else()
    set(MACOS TRUE)
    if(MAMA_CMAKE_ARCH MATCHES "x86_64") # (older x64)
        set(MAMA_ARCH_X64 TRUE)
        {get_build_dir_defines(c.build_dir_macos64())}
    elseif(MAMA_CMAKE_ARCH MATCHES "(arm64)|(ARM64)") # (M1 and later)
        set(MAMA_ARCH_ARM64 TRUE)
        {get_build_dir_defines(c.build_dir_macosarm64())}
    else()
        message(FATAL_ERROR "MAMA: Unrecognized macOS architecture '${{MAMA_CMAKE_ARCH}}'")
    endif()
  endif()
elseif(RASPI)
        set(MAMA_ARCH_ARM32 TRUE)
        {get_build_dir_defines(c.build_dir_raspi32())}
elseif(OCLEA)
        set(MAMA_ARCH_ARM64 TRUE)
        set(YOCTO_LINUX ON)
        add_compile_definitions(OCLEA=1)
        {get_build_dir_defines(c.build_dir_oclea64())}
elseif(XILINX)
        set(MAMA_ARCH_ARM64 TRUE)
        set(YOCTO_LINUX ON)
        add_compile_definitions(XILINX=1)
        {get_build_dir_defines(c.build_dir_xilinx64())}
elseif(IMX8MP)
        set(MAMA_ARCH_ARM64 TRUE)
        set(YOCTO_LINUX ON)
        add_compile_definitions(IMX8MP=1)
        {get_build_dir_defines(c.build_dir_imx8mp())}
elseif(MIPS)
        set(MAMA_ARCH_MIPS TRUE)
        add_compile_definitions(MIPS=1)
        {get_build_dir_defines(c.build_dir_mips())}
elseif(UNIX)
    set(LINUX TRUE)
    if(MAMA_CMAKE_ARCH MATCHES "(amd64)|(AMD64)|(IA64)|(x86_64)")
        set(MAMA_ARCH_X64 TRUE)
        {get_build_dir_defines(c.build_dir_linux64())}
    elseif(MAMA_CMAKE_ARCH MATCHES "(X86)|(x86)|(i386)|(i686)")
        set(MAMA_ARCH_X86 TRUE)
        {get_build_dir_defines(c.build_dir_linux32())}
    elseif(MAMA_CMAKE_ARCH MATCHES "(aarch64)|(AARCH64)|(arm64)|(ARM64)")
        set(MAMA_ARCH_ARM64 TRUE)
        {get_build_dir_defines(c.build_dir_linuxarm64())}
    else()
        message(FATAL_ERROR "MAMA: Unrecognized Linux architecture '${{MAMA_CMAKE_ARCH}}'")
    endif()
else()
    message(FATAL_ERROR "mama build: Unsupported Platform! '${{MAMA_CMAKE_ARCH}}'")
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
    All dependencies must be resolved at this stage.

    With parallel_load=True, parents submit child loads to this executor and
    then block on their futures while still holding a worker slot. The default
    ThreadPoolExecutor() is bounded (~min(32, cpu_count+4)) so a moderately
    deep dep tree can starve waiting for slots. We pick a max_workers high
    enough that this doesn't happen for any realistic project.

    For `update` runs we auto-enable parallel_load so concurrent git fetches
    share an SSH multiplexed master. The actual fetch concurrency is capped
    at `parallel_max` (default 20) by a semaphore inside Git.run_git.

    NOTE on parallel_load: existing helpers like BuildDependency.add_child
    and BuildDependency.load are not strictly thread-safe (the existing
    `currently_loading` busy-wait has a TOCTOU window). For most projects
    this is benign because concurrent loads of the SAME dep are rare. Pass
    `serial` on the command line to disable parallel loading if you hit
    issues.
    """
    # Parallel by default (`serial` opts out): load() + add_child are now thread-safe.
    if not root.config.serial_load:
        root.config.parallel_load = True

    ssh_multiplex.init_fetch_semaphore(root.config.parallel_max)

    root.config.update_stats.start()
    with concurrent.futures.ThreadPoolExecutor(max_workers=256) as e:
        def load_dependency(dep: BuildDependency):
            if dep.already_loaded:
                return dep.should_rebuild

            changed = dep.load()
            if dep.config.parallel_load:
                futures = []
                for child in dep.get_children():
                    futures.append(e.submit(load_dependency, child))
                for f in futures:
                    changed |= f.result()
            else:
                for child in dep.get_children():
                    changed |= load_dependency(child)

            dep.after_load()
            return changed
        load_dependency(root)
    root.config.update_stats.stop()
    summary = root.config.update_stats.summary_line()
    if summary and root.config.print:
        console(f'  {summary}', color=Color.BLUE)


def print_dependencies(root: BuildDependency):
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


def execute_task_chain(flat_deps_reverse: List[BuildDependency]):
    for dep in flat_deps_reverse:
        if not os.path.exists(_mama_cmake_path(dep)):
            _save_mama_cmake_and_dependencies_cmake(dep) # save a dummy mama.cmake before build

        if dep.config.verbose:
            console(f'  - Execute Tasks: {dep.name}', color=Color.BLUE)

        # validate we're not building twice
        if dep.already_executed:
            error(f"Critical Error: '{dep.name}' executed by child project")
            raise RuntimeError(f"Cyclical Dependency detected for '{dep.name}'")

        # go through all child deps and make sure they executed
        for c in dep.get_children():
            if not c.already_executed:
                error(f"Critical Error: child '{c.name}' has not been executed before executing target '{dep.name}'")
                raise RuntimeError(f"Child target not executed before target which requires it: {c.name}")

        _save_mama_cmake_and_dependencies_cmake(dep)
        dep.target._execute_tasks()

        # saves a helper autocomplete includes txt file to make adding .vscode include paths easier
        _save_vscode_compile_commands(dep)

        if dep.config.verbose and not dep.config.test:
            if dep.is_root_or_config_target():
                print_dependencies(dep)
            # else:
            #     print_dependencies(dep) # TODO: different output for non-root targets


def _make_display(config):
    import sys, shutil, time
    from .utils.build_display import BuildDisplay
    out = sys.stdout
    isatty = bool(getattr(out, 'isatty', lambda: False)())
    return BuildDisplay(out, isatty=isatty, clock=time.monotonic,
                        term_size=lambda: tuple(shutil.get_terminal_size((100, 24))),
                        verbose=config.verbose)


# Shared by the two parallel runners (execute_task_chain_parallel, execute_unified).
def _phase_label(dep, kind) -> str:
    # 'load' opens optimistically (clone if no tree yet, else check) then _run_phase relabels it to
    # what load() actually did (dep.load_action: check/clone/pulling); others show verbatim.
    if kind == 'load': return 'clone' if not dep.is_real_clone() else 'check'
    return kind


def _run_phase(display, dep, kind, body, build_slot, detail='', final=False):
    """Run one scheduler phase for `dep` on its single dep-level display task (keyed by name so all
    phases share one line): route this thread's console output + subprocess CPU + build barrier into
    it, run `body(sink)`, then end the phase. `final=True` (the build) commits the merged summary."""
    tid = dep.name
    sink = lambda line: display.feed(tid, line)
    name = f'{_node_marker(dep)} {dep.name}' if dep.config.verbose else dep.name  # tree markers: verbose only
    display.start_task(tid, _phase_label(dep, kind), name, detail)
    ok = False; t0 = time.monotonic()
    try:
        with system.capture_to(sink, display, tid, build_slot):  # console + CPU + build barrier
            body(sink)
        ok = True
    finally:
        pt = getattr(dep, 'phase_times', None)  # accumulate for the `buildtimes` breakdown
        if pt is not None: pt[kind] = pt.get(kind, 0.0) + (time.monotonic() - t0)
        if kind == 'load': display.relabel(tid, dep.load_action)  # reflect what load() actually did
        display.finish_task(tid, ok, final)


def _configure_body(dep, sink):
    _save_mama_cmake_and_dependencies_cmake(dep)  # children built -> their exports are ready
    dep.target.configure_phase(out=sink)


def _build_body(dep, sink):
    dep.target.build_phase(out=sink)
    dep.already_executed = True
    _save_vscode_compile_commands(dep)


def _stable_cpu_sampler(measure, clock, window=0.5):
    """Gate `measure()` (CPU% since its last call) to >=`window`-second re-samples, caching between.
    The scheduler polls at irregular sub-100ms-to-1s gaps; over a tiny window cpu_percent(interval=None)
    reads a meaningless spiky 0% or 100%, so only re-measure once a real window has elapsed."""
    state = {'t': clock(), 'val': 0.0}
    def sample():
        now = clock()
        if now - state['t'] >= window:
            state['val'] = measure(); state['t'] = now
        return state['val']
    return sample


# Build-job overprovisioning (max reserved cores = core_budget * this). MSVC/MSBuild tolerates 2x; on Linux
# the build is memory-bound (below) - GCC/make already saturates the cores - so overprovisioning beyond the
# RAM-capped budget only risks OOM. _GB_PER_COMPILE is a heavy-C++ TU's peak RSS; total RAM / it caps how
# many parallel compiles we allow so a swarm can't take down a memory-limited box (a WSL-killer).
_OVERPROVISION_WIN, _OVERPROVISION_UNIX = 2.0, 1.0
_GB_PER_COMPILE = 1.5


def _mem_capped_budget(jobs: int) -> int:
    """Cap the core budget by RAM so parallel heavy C++ compiles can't OOM. Never below 1 or above `jobs`."""
    import psutil
    gb = psutil.virtual_memory().total / (1024 ** 3)
    return max(1, min(jobs, int(gb / _GB_PER_COMPILE)))


def _make_scheduler(config, **extra):
    """The build Scheduler with a stable psutil CPU sampler and the Ctrl+C child-killer."""
    import psutil, time
    from .build_scheduler import Scheduler
    cpu = psutil.cpu_count() or 4
    psutil.cpu_percent(interval=None)  # prime the sampler (first call always returns 0.0)
    win = system.System.windows
    budget = config.jobs if win else _mem_capped_budget(config.jobs)  # Linux: don't OOM on parallel C++ compiles
    extra.setdefault('overprovision', _OVERPROVISION_WIN if win else _OVERPROVISION_UNIX)
    return Scheduler(max_configure=min(cpu * 2, 32), core_budget=budget, abort_hook=SubProcess.terminate_all,
                     cpu_sampler=_stable_cpu_sampler(lambda: psutil.cpu_percent(interval=None), time.monotonic),
                     **extra)


_BUILD_TIMES_FILE = '.mama_buildtimes.json'  # per-workspace cache of measured build seconds (critical-path scheduling)
_SEC_PER_TU = 0.5  # first-build cost proxy for a dep with no measured history yet


def _build_times_path(root):
    bd = getattr(root, 'build_dir', None)  # None for artifactory-only roots / test fakes -> caching is skipped
    return os.path.join(os.path.dirname(os.path.dirname(bd)), _BUILD_TIMES_FILE) if isinstance(bd, str) and bd else None


def _load_build_times(root) -> dict:
    import json
    path = _build_times_path(root)
    if not path: return {}
    try:
        with open(path, encoding='utf-8') as f: return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_build_times(deps, root):
    """Persist each dep's measured build wall seconds so the next run computes accurate critical paths."""
    import json
    path = _build_times_path(root)
    if not path: return
    times = _load_build_times(root)
    for d in deps:
        pt = getattr(d, 'phase_times', None)
        if pt and pt.get('build'): times[d.name] = round(pt['build'], 2)
    try:
        with open(path, 'w', encoding='utf-8') as f: json.dump(times, f)
    except OSError:
        pass


def _build_cost_fn(root):
    """est build seconds per dep for critical-path priority: the last measured time if known, else a
    TU-count proxy (same seconds unit, so the bottom-level sums stay consistent)."""
    cached = _load_build_times(root)
    def cost(dep):
        t = cached.get(dep.name)
        if t: return float(t)
        try: return max(1.0, dep.target._count_tu()[0] * _SEC_PER_TU)
        except Exception: return 1.0
    return cost


def _handle_failure(display, failed):
    """First failed job -> replay its captured output (TTY) + traceback, then exit nonzero. A Ctrl+C
    abort prints a terse interrupted line (no replay/traceback) and still exits nonzero."""
    import traceback
    if isinstance(failed.error, KeyboardInterrupt):
        console('  [BUILD INTERRUPTED]  stopped by Ctrl+C', color=Color.RED)
        exit(-1)
    console(f'  [BUILD FAILED]  {failed.node.name}', color=Color.RED)
    if display.isatty:  # non-TTY already dumped the output on finish
        display.replay(failed.node.name)
    if failed.error:
        console(''.join(traceback.format_exception(type(failed.error), failed.error, failed.error.__traceback__)))
    exit(-1)


def _deploy_run_postpass(deps, config):
    """Deploy/run/test post-pass: target-specific, cheap, kept serial and children-first."""
    for dep in deps:
        dep.target._execute_deploy_tasks()
        dep.target._execute_run_tasks()
        if config.verbose and not config.test and dep.is_root_or_config_target():
            print_dependencies(dep)


def execute_task_chain_parallel(flat_deps_reverse: List[BuildDependency]):
    """Parallel counterpart of execute_task_chain: a DAG scheduler runs each dep's configure and
    build as separate jobs (configure waits on children's builds). Deploy/run/test stay serial."""
    import time
    from .build_scheduler import build_dep_jobs
    deps = list(flat_deps_reverse)
    config = deps[0].config
    root = next((d for d in deps if getattr(d, 'is_root', False)), deps[-1])
    display = _make_display(config)
    sched = _make_scheduler(config, pending_log=display.set_pending)
    cfg = lambda d: _run_phase(display, d, 'configure', lambda s: _configure_body(d, s), sched.build_slot)
    bld = lambda d: _run_phase(display, d, 'build', lambda s: _build_body(d, s), sched.build_slot,
                               _build_detail(d), final=True)  # build is the dep's last phase -> commit its summary
    # cost_fn sets critical-path priorities so a long-pole dep launches first instead of waiting behind cheaper ones
    jobs = build_dep_jobs(deps, cfg, bld, weight_fn=_reserve_weight, cost_fn=_build_cost_fn(root))
    system.set_active_display(display)
    start = time.monotonic()
    with _build_insights_session(config, root):  # MSVC buildtimes: wrap the build in a vcperf trace (else no-op)
        try:
            failed = sched.run(jobs)
        finally:
            display.close()
            system.set_active_display(None)
            SubProcess.clear_abort()  # re-arm spawning (run() returned -> all workers drained)
    if failed is not None: _handle_failure(display, failed)
    _print_build_summary(deps, time.monotonic() - start)
    _save_build_times(deps, root)  # feed the next run's critical-path scheduling
    if getattr(config, 'buildtimes', False):
        print_buildtimes(deps)
        _print_build_insights(config, deps)
    _deploy_run_postpass(flat_deps_reverse, config)


def _reserve_weight(dep) -> int:
    """Cores reserved for a build job AT LAUNCH. The root is ungated and runs alone, and a custom
    build() reserves from inside cmake_build() (the barrier): both launch free (0). A default build
    reserves its capped cores."""
    if dep.is_root or dep.target._has_custom_build(): return 0
    return dep.target._reserved_cores()


def _build_detail(dep) -> str:
    cores = dep.config.jobs if dep.is_root else dep.target._reserved_cores()  # root runs alone at full -j
    return f'J{cores:<2}'


def _node_marker(dep) -> str:
    """[R]oot / [L]eaf (no deps of its own) / [T]runk (has deps) - quick visual of tree position."""
    if getattr(dep, 'is_root', False): return '[R]'
    return '[L]' if not dep.get_children() else '[T]'


def print_sched_debug(root: BuildDependency):
    """TEMP diagnostic (CLI: sched_debug): print each target's build-weight calc WITHOUT building.
    Reads existing build-dir artifacts, so it runs in seconds for fast iteration on the TU probe."""
    deps = get_flat_deps(root)
    console(f'  {"target":<22}{"TU":>6}  {"via":<16}{"probe":>6}{"reserve":>9}{"-j":>5}   flags', color=Color.BLUE)
    for d in deps:
        t = d.target
        try: tu, via = t._count_tu()
        except Exception as e: tu, via = -1, f'ERR:{type(e).__name__}'
        probe = t._probe_build_jobs()
        reserve = t._reserved_cores()  # canonical reserve (== actual -j); was a stale jobs//2 formula
        flags = []
        if t._has_custom_build(): flags.append('custom-build')   # -> configure skips probe -> -j=config.jobs
        if getattr(d, 'nothing_to_build', False): flags.append('nothing_to_build')
        if getattr(d, 'from_artifactory', False): flags.append('artifactory')
        console(f'  {d.name:<22}{tu:>6}  {via:<16}{probe:>6}{reserve:>9}{probe:>5}   {" ".join(flags)}')


def _print_build_summary(deps, elapsed: float):
    """End-of-session line: how many targets actually compiled (cached/artifactory ones excluded)."""
    built = sum(1 for d in deps if getattr(d, 'should_rebuild', False)
                and not getattr(d, 'from_artifactory', False) and not getattr(d, 'nothing_to_build', False))
    console(f'Built {built} target(s) in {get_time_str(elapsed)}', color=Color.GREEN)


# buildtimes (stage 1): a normalized horizontal bar per package, segmented load/configure/build.
_BAR_FILL = 40  # the slowest package fills this width; the rest scale down proportionally
_BUILDTIMES_FLOOR = 0.33  # omit packages faster than this - they're noise on the chart
_BAR = (('load', Color.BLUE), ('configure', Color.MAGENTA), ('build', Color.GREEN))
_GLYPHS_SHADE = ('░', '▒', '▓')  # light/medium/dark blocks (UTF-8 terminals)
_GLYPHS_ASCII = ('-', '=', '#')  # legacy code-page fallback (Windows cp1252 can't encode the blocks)
_glyphs_cache = None


def _can_encode_blocks(encoding) -> bool:
    try: ''.join(_GLYPHS_SHADE).encode(encoding); return True
    except (UnicodeEncodeError, LookupError): return False


def _bar_glyphs():
    """Block shades on a UTF-8 terminal, ASCII on a legacy code page. Decided ONCE - the output
    encoding is constant per process, so there's no point re-testing it per report or per row."""
    global _glyphs_cache
    if _glyphs_cache is None:
        _glyphs_cache = _GLYPHS_SHADE if _can_encode_blocks(getattr(sys.stdout, 'encoding', None) or 'ascii') \
                        else _GLYPHS_ASCII
    return _glyphs_cache


def _buildtimes_bar(times: dict, total: float, max_total: float, glyphs) -> str:
    """Bar whose length scales with total/max_total; inside, load/configure/build take shares of that
    length (shaded, coloured). Right-padded to full width so the trailing total aligns across rows."""
    bar_len = max(1, round(total / max_total * _BAR_FILL)) if max_total > 0 else 0
    out, used, last = [], 0, len(_BAR) - 1
    for i, ((kind, color), ch) in enumerate(zip(_BAR, glyphs)):
        n = (bar_len - used) if i == last else min(round(times.get(kind, 0.0) / total * bar_len), bar_len - used)
        if n > 0: out.append(get_colored_text(ch * n, color))
        used += n
    return ''.join(out) + ' ' * (_BAR_FILL - bar_len)


def print_buildtimes(deps):
    """`buildtimes`: one normalized bar per package (load / configure / build), slowest first, with its
    total wall time. Packages faster than _BUILDTIMES_FLOOR seconds are omitted so the chart stays relevant."""
    label = 'Build times'
    rows = []
    for d in deps:
        pt = getattr(d, 'phase_times', None)
        if not pt: continue
        total = sum(pt.values())  # once per dep, not recomputed in a filter
        if total >= _BUILDTIMES_FLOOR: rows.append((d.name, pt, total))
    if not rows: return
    rows.sort(key=lambda r: r[2], reverse=True)
    max_total = rows[0][2]
    name_w = max(min(max(len(name) for name, _, _ in rows), 24), len(label))  # fit the names AND the header label
    glyphs = _bar_glyphs()
    legend = '  '.join(get_colored_text(f'{ch} {kind}', color) for (kind, color), ch in zip(_BAR, glyphs))
    console(f'\n  {label:<{name_w}}  {legend}')  # label padded to the name column so the legend sits over the bars
    for name, pt, total in rows:
        console(f'  {name:<{name_w}.{name_w}}  {_buildtimes_bar(pt, total, max_total, glyphs)}  {get_time_str(total)}')


def _build_insights_session(config, root: BuildDependency):
    """MSVC `buildtimes`: a live vcperf /timetrace session wrapping the build. Linux `buildtimes`: record the
    build start time so the post-build report collects only the clang -ftime-trace JSONs written this run.
    Otherwise a null context."""
    import contextlib, time
    if not getattr(config, 'buildtimes', False):
        return contextlib.nullcontext()
    if config.msvc:
        from .build_insights import find_vcperf, VcPerfSession, timetrace_path
        vcperf = find_vcperf(config)
        if not vcperf:
            warning('buildtimes: vcperf.exe not found (set VCPERF= or run from a Developer Command Prompt);'
                    ' skipping MSVC Build Insights')
            return contextlib.nullcontext()
        config._timetrace_json = timetrace_path(root.build_dir)
        return VcPerfSession(vcperf, config._timetrace_json)
    config._buildtimes_start = time.time()  # wall start: post-build we analyze only traces newer than this
    return contextlib.nullcontext()


def _insights_target(config, deps):
    """(scope-label, scoped-dep-or-None) for the deep report: the named <target>, else whole-build 'root'."""
    if config.has_target() and not config.targets_all():
        dep = next((d for d in deps if d.name.lower() == config.target.lower()), None)
        if dep: return dep.name, dep
    return 'root', None


def _print_build_insights(config, deps):
    """After the Stage 1 bars: the compiler-specific deep dive. MSVC -> the vcperf trace; Linux/Clang -> the
    clang -ftime-trace JSONs written this build; Linux/GCC -> a note (GCC has no per-file trace)."""
    label, dep = _insights_target(config, deps)
    if config.msvc:
        _print_msvc_insights(config, label, dep)
    elif getattr(config, '_buildtimes_start', None) is not None:
        _print_clang_insights(config, deps, label, dep)


def _print_msvc_insights(config, label, dep):
    path = getattr(config, '_timetrace_json', None)
    if not path or not os.path.exists(path): return
    import json
    from .build_insights import parse_timetrace, print_buildtimes_deep
    scope_paths = [p for p in (getattr(dep, 'src_dir', None), getattr(dep, 'build_dir', None)) if p] if dep else None
    try:
        with open(path, encoding='utf-8') as f: data = json.load(f)
        stats = parse_timetrace(data, scope_paths)
    except Exception as e:
        warning(f'buildtimes: failed to read vcperf trace: {e}'); return
    print_buildtimes_deep(stats, label)


def _print_clang_insights(config, deps, label, dep):
    import time
    from .build_insights import collect_clang_traces, parse_clang_traces, print_buildtimes_deep
    if not config.clang:
        warning('buildtimes: deep per-file insights need Clang -ftime-trace; build with `clang` for the breakdown')
        return
    start = config._buildtimes_start
    scoped = [dep] if dep else deps  # a <target> -> just its build dir; else every package's
    paths = []
    for d in scoped:
        bd = getattr(d, 'build_dir', None)
        if bd: paths += collect_clang_traces(bd, since=start)
    stats = parse_clang_traces(paths, wall_s=time.time() - start)
    print_buildtimes_deep(stats, f'{label} (clang)')


def execute_unified(root: BuildDependency):
    """Dynamic DAG scheduler interleaving cloning with configure+build: each dep is a LOAD job whose
    completion GROWS the graph with its children's LOAD/CONFIGURE/BUILD jobs; a dep's CONFIGURE waits
    on its own LOAD + its children's BUILDs. So leaf nodes build while deeper deps still clone. Used
    for a plain full build (main() falls back to the old path otherwise); deploy/run/test stay serial."""
    import time
    from .build_scheduler import Job, LOAD, CONFIGURE, BUILD, assign_priorities
    config = root.config
    ssh_multiplex.init_fetch_semaphore(config.parallel_max)
    config.update_stats.start()
    display = _make_display(config)
    sched = _make_scheduler(config, max_load=config.parallel_max, pending_log=display.set_pending)
    load_jobs: dict = {}; cfg_jobs: dict = {}; bld_jobs: dict = {}  # dep -> Job (mutated under sched lock)
    cost = _build_cost_fn(root)
    job_cost = lambda j: cost(j.node) if j.kind == BUILD else 0.0  # configure ~free; the build cost drives the path

    def make_jobs(dep, parent_load):
        L = Job((dep, 'L'), LOAD, (lambda d=dep: _do_load(d)), deps=({parent_load} if parent_load else set()), node=dep)
        C = Job((dep, 'C'), CONFIGURE, (lambda d=dep: _do_configure(d)), deps={L}, node=dep)
        B = Job((dep, 'B'), BUILD, (lambda d=dep: _do_build(d)), deps={C}, node=dep,
                weight=(lambda d=dep: _reserve_weight(d)), ungated=getattr(dep, 'is_root', False))
        load_jobs[dep] = L; cfg_jobs[dep] = C; bld_jobs[dep] = B
        return [L, C, B]

    def _do_load(dep):
        def body(sink):
            dep.load()  # clone + parse mamafile + dependencies() -> populates dep.children (no recursion)
            def grow():  # runs under the scheduler lock: safe to mutate registries + add edges
                new = []
                for child in dep.get_children():
                    if child not in load_jobs:
                        new += make_jobs(child, load_jobs[dep])
                cfg_jobs[dep].deps.update(bld_jobs[c] for c in dep.get_children())  # configure waits on child builds
                assign_priorities(list(cfg_jobs.values()) + list(bld_jobs.values()), job_cost)  # re-rank critical path
                return new
            sched.grow(grow)
        _run_phase(display, dep, 'load', body, sched.build_slot)

    def _do_configure(d): _run_phase(display, d, 'configure', lambda s: _configure_body(d, s), sched.build_slot)
    def _do_build(d):
        _run_phase(display, d, 'build', lambda s: _build_body(d, s), sched.build_slot, _build_detail(d), final=True)

    system.set_active_display(display)
    start = time.monotonic()
    with _build_insights_session(config, root):  # MSVC buildtimes: wrap the build in a vcperf trace (else no-op)
        try:
            failed = sched.run(make_jobs(root, None))
        finally:
            display.close()
            system.set_active_display(None)
            config.update_stats.stop()
            SubProcess.clear_abort()  # re-arm spawning (run() returned -> all workers drained)
    if failed is not None: _handle_failure(display, failed)
    flat = get_flat_deps(root)
    _print_build_summary(flat, time.monotonic() - start)
    _save_build_times(flat, root)  # feed the next run's critical-path scheduling
    if getattr(config, 'buildtimes', False):
        print_buildtimes(flat)
        _print_build_insights(config, flat)
    _deploy_run_postpass(reversed(flat), config)


def find_dependency(root: BuildDependency, name: str) -> BuildDependency:
    """ This is mainly used for finding root target or specific command line target """
    if root.name.lower() == name.lower():
        return root
    for dep in root.get_children():
        found = find_dependency(dep, name)
        if found: return found
    return None
