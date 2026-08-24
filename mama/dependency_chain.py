import os, sys, shutil, time, contextlib, threading, concurrent.futures
from typing import List

from mama.build_config import BuildConfig
from .build_dependency import BuildDependency
from ._version import __version__
from .buildsys.cmake.mamacmake import MAMA_CMAKE_HEADER, mama_cmake_text
from . import package
from .platforms.windows import msvc_toolset_version
from .utils.errors import BuildError
from .utils.fileio import read_lines_from, read_text_from, write_text_to, save_file_if_contents_changed
from .utils.paths import MAMA_SHIM_FILENAME, path_join
from .utils.progress import get_time_str
from . import build_names
from .utils import abort, ssh_multiplex, system
from .utils.sub_process import SubProcess
from .utils.system import Color, console, error, warning, get_colored_text


def _get_cmake_path_list(paths):
    return ''.join(f'\n    "{path}"' for path in paths)


def _get_exported_libs(target):
    """The exported libs this platform can link. A versioned ELF name (libfoo.so.1.2.3) is a
    real link target, so it passes where a plain .so does."""
    allowed = target.config.platform.lib_extensions()
    versioned = '.so' in allowed
    return [lib for lib in target.exported_libs if lib and
            (lib.endswith(allowed) or (versioned and '.so.' in lib and lib[-1].isdigit()))]


def _get_hierarchical_libs(root: BuildDependency):
    """Exported libs of `root` and every dep below it, in Unix link order: a lib comes after everything
    that references it. get_flat_deps dedups keep-last, so a shared dep appears once, not once per path."""
    deps = []
    syslibs = []
    for dep in get_flat_deps(root):
        deps += _get_exported_libs(dep.target)
        syslibs += dep.target.exported_syslibs
    return deps + syslibs


def _get_flattened_deps(root: BuildDependency):
    # a Unix linker needs [parent] [child] order
    ordered = []
    def add_unique_items(deps: List[BuildDependency]):
        for child in deps:
            if child in ordered: # already listed: move it lower
                ordered.remove(child)
            ordered.append(child)
            add_unique_items(child.get_children())
    add_unique_items(root.get_children())
    return ordered


def get_flat_deps(root: BuildDependency):
    """ Return the flat deps, including root. """
    return [root] + _get_flattened_deps(root)


def get_flat_child_deps(dep: BuildDependency):
    """ Return the flat child deps of dep, without dep itself. """
    return _get_flattened_deps(dep)


def mark_unbuilt_target_deps(root: BuildDependency, config: BuildConfig):
    """`build target=X` builds only X, so revive unusable or stale deps below X. Deepest-first lets a
    changed leaf rebuild its source-built parents before X consumes them. The scope stays X's subtree:
    a wider mark could re-enter a mamafile that runs `mama build target=Y` itself."""
    target = find_dependency(root, config.target)
    if target is None: return
    for dep in reversed(get_flat_child_deps(target)):
        changed_child = None if dep.is_artifactory_shim() else \
                        next((child for child in dep.get_children() if child.should_rebuild), None)
        stale = dep.has_stale_locked_artifacts()
        if dep.should_rebuild or (dep.has_usable_artifacts() and not stale and not changed_child): continue
        dep.should_rebuild = True
        if config.print:
            reason = 'locked commit changed' if stale else \
                     (f'{changed_child.name} changed' if changed_child else 'not built yet')
            warning(f'  - Target {dep.name: <16} BUILD [{reason}]')


@contextlib.contextmanager
def load_display(config):
    """The live region of the load phase, for the classic path. execute_unified draws its own, and the
    root already loaded, so nothing this region hides decides the toolchain."""
    display = _make_display(config)
    system.set_active_display(display)  # an ownerless console() line lands above the region, not through it
    try:
        yield display
    finally:
        display.close()
        system.set_active_display(None)


def load_root(root: BuildDependency):
    """Load the root before any other dep. Its settings() locks the compiler that names every dep dir
    below it. Its mamafile names the workspace that holds the build log. The output of that load
    reaches the terminal directly, so a mis-picked toolchain never hides inside a display."""
    try:
        root.load()
    except BuildError as err:  # the same report the full load prints, see load_dependency_chain
        _report_error(err, root.config.verbose)
        exit(-1)


def load_path_to_target(root: BuildDependency):
    """Stage one of a targeted load: skim the cheapest dep next and stop once the graph names the
    target. A skim names the children of a dep and nothing more, so this stage fetches nothing,
    clones nothing and creates no build dir. A branch the walk never enters stays unread.

    Keep the walk serial. It stops early, and a parallel wave reads the branches the stop skips."""
    config = root.config
    def cost(dep: BuildDependency) -> int:
        """Ranks a dep by what its load reveals. A deferred load names no child, so it reads last."""
        if dep.dep_source.is_src: return 0
        return 1 if dep.is_real_clone() else 2
    found = config.target_matches(root.name)
    queue, seen = [], set()   # the frontier of (cost, dep), and the ids it already holds
    def enqueue(deps):
        """A load names its children here, so the walk tests the new names here too."""
        nonlocal found
        for dep in deps:
            if id(dep) in seen: continue
            seen.add(id(dep))
            found = found or config.target_matches(dep.name)
            queue.append((cost(dep), dep))
    try:
        root.load()  # the root load locks the compiler, which every dep dir name below depends on
        enqueue(root.get_children())
        while queue and not found:
            abort.check()  # a queued read must not start during a build abort
            entry = min(queue, key=lambda e: e[0])   # equal cost keeps the order the mamafile declared
            queue.remove(entry)
            entry[1].skim()
            enqueue(entry[1].get_children())
    except BuildError as err:  # the same report the full load prints, see load_dependency_chain
        _report_error(err, config.verbose)
        exit(-1)


def revive_deferred_target_deps(root: BuildDependency, config: BuildConfig, display=None):
    """Stage two of a targeted load: load the subtree of the target, which stage one stopped short of,
    and revive the deferred deps inside it. A dep outside that subtree stays unloaded and never builds."""
    target = find_dependency(root, config.target)
    if target is None: return
    load_dependency_chain(target, display)
    reload_deferred_deps(target, display=display)


def reload_deferred_deps(scope: BuildDependency, free_only=False, display=None) -> bool:
    """Load every deferred dep under `scope`. A reload can discover new children, so loop until the
    scope holds no deferred dep. Return True when this call revived a dep.
    free_only: revive only the deps a cached package answers, so the pass costs no network
    display: the live region of the load phase, so a revived dep draws its line there too"""
    revived = False
    while True:
        deferred = [d for d in get_flat_deps(scope) if d.load_deferred and (not free_only or d.load_is_free())]
        if not deferred: return revived
        revived = True
        for d in deferred: d.revive_deferred_load()
        load_dependency_chain(scope, display)
        # That walk stops at every loaded dep it meets, so a revived dep under one never reloaded, and
        # it would export nothing. Enter a walk AT each dep the scope walk could not reach.
        for d in deferred:
            if not d.already_loaded: load_dependency_chain(d, display)


# files mama writes into a build dir - one must be present before the sweep will delete a directory
_BUILD_DIR_MARKERS = ('CMakeCache.txt', 'mama-dependencies.cmake', 'mama_exported_libs', MAMA_SHIM_FILENAME,
                      'papa.txt', 'git_status', 'mamafile_tag')


def sweep_orphaned_build_dirs(root: BuildDependency, config: BuildConfig) -> int:
    """`clean all` must clean EVERYTHING for this platform, but the tree walk cannot reach a dep whose
    source is gone and so declares no children. Enumerate from disk instead, and delete only dirs that
    carry a mama marker file. A dep added with args names its own build dirs (linux, linux-lgpl), so
    build_names decides which dirs belong to this config. Return the removed count."""
    workspace = os.path.dirname(root.dep_dir)
    config_dir = root.build_dir_name  # the root carries no dep args, so this is the config's own dir name
    removed = 0
    try: names = os.listdir(workspace)
    except OSError: return 0
    for name in names:
        dep_dir = path_join(workspace, name)
        try: subdirs = os.listdir(dep_dir)
        except OSError: continue  # a file in the workspace, or a dir mama may not read
        for sub in subdirs:
            build_dir = path_join(dep_dir, sub)
            if not build_names.is_build_dir_of(sub, config_dir) or not os.path.isdir(build_dir): continue
            if not any(os.path.exists(os.path.join(build_dir, m)) for m in _BUILD_DIR_MARKERS): continue
            if config.print: console(f'  - Target {name: <16} CLEAN  {sub} (orphaned)')
            shutil.rmtree(build_dir, ignore_errors=True)
            removed += 1
    return removed


def get_deps_only_targets(root: BuildDependency, deps_only_target_name: str, config: BuildConfig):
    """For `deps_only` with a target name, return (flat_deps, flat_deps_reverse) of only that target's
    deps. Also mark those deps for rebuild, and clean them when needed."""
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


class DepsOnlyScope:
    """`deps_only` for the unified scheduler: which deps may configure and build. Every dep still LOADs,
    because only a load discovers the graph. The scope root is the named target, else the project root,
    and only the deps below it build. A named target also forces its deps to rebuild, and a `rebuild`
    cleans them first. This matches get_deps_only_targets on the classic path."""

    def __init__(self, config: BuildConfig, target_name: str = None):
        self.config = config
        self.target_name = target_name.lower() if target_name else None
        self.found = target_name is None  # a named target must appear somewhere in the tree
        self._under = {}  # dep -> True when the dep is the scope root or below it

    def _is_scope_root(self, dep: BuildDependency) -> bool:
        return dep.name.lower() == self.target_name if self.target_name else dep.is_root

    def enter(self, dep: BuildDependency, parent: BuildDependency):
        """Place `dep` relative to the scope root. Call once, when the scheduler discovers the dep."""
        at_root = self._is_scope_root(dep)
        if at_root: self.found = True
        self._under[dep] = at_root or bool(parent and self._under.get(parent))

    def is_inside(self, dep: BuildDependency) -> bool:
        """True if `dep` is the scope root or below it, so every child of `dep` builds."""
        return self._under.get(dep, False)

    def includes(self, dep: BuildDependency) -> bool:
        """True if `dep` itself builds: below the scope root, and not the scope root."""
        return self.is_inside(dep) and not self._is_scope_root(dep)

    def promote(self, dep: BuildDependency) -> List[BuildDependency]:
        """Mark a shared dep, first found outside the scope and now reached through it, plus everything
        already found below it. Return the deps that gained build work."""
        if self.is_inside(dep): return []
        self._under[dep] = True
        gained = [dep]
        for child in dep.get_children(): gained += self.promote(child)
        return gained

    def prepare(self, dep: BuildDependency):
        """`deps_only <target>` rebuilds that target's deps: force the rebuild before configure, and clean
        first on a `rebuild`. With no target the normal up-to-date check keeps an unchanged dep cached."""
        if not self.target_name: return
        if self.config.clean:
            dep.clean()
            dep.create_build_dir_if_needed()
        dep.should_rebuild = True

    def widen_for_deploy(self):
        """After the build, deploy and upload still gate on `config.target`, which names the excluded
        target, so widen it. main() does the same reset on the classic path."""
        if self.target_name: self.config.target = 'all'


def get_deps_that_depend_on_target(root: BuildDependency, target: BuildDependency, deps = []) -> List[BuildDependency]:
    """ Return all dependencies that depend on the target. """
    discovered_new = False
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

    # expand the initial deps to include second-level dependencies
    while discovered_new:
        discovered_new = False
        for d in deps:
            depth_first_search_for_target(d)
            if discovered_new:
                break # restart the outer loop
    return deps


def _proxy_paths(dep: BuildDependency) -> list:
    """Every path this dep needs a `mama.cmake` proxy at, named by the `include()` commands of its
    CMakeLists.txt or of one it adds. A dep that names none, and whose shape needs a proxy, takes the
    path beside that file. A leaf that names none gets nothing, having no includes or libs to name."""
    if not dep.src_dir or not dep.cmakelists_exists(): return []
    paths = dep.mama_cmake_paths()
    if paths: return paths
    return [dep.default_mama_cmake_path()] if dep.get_children() and dep.mamafile_exists() else []


def _needs_mama_cmake(dep: BuildDependency) -> bool:
    return bool(_proxy_paths(dep))


def ensure_mama_cmake(dep: BuildDependency):
    """Write the proxy at every path this dep needs one, and check each write. The `configure()` hook can
    move `cmake_lists_path`, so the cmake configure step calls this again before cmake reads that file."""
    for path in _proxy_paths(dep):
        _save_mama_cmake(dep, path)
        if not os.path.exists(path):
            raise BuildError(f'{dep.name}: mama wrote no {path} for {dep.cmakelists_path()}.' + \
                             ' Every MAMA_ variable would expand to an empty string.')


def _save_cmake_files(root: BuildDependency):
    """`<build_dir>/mama-dependencies.cmake` for every dep, and the `mama.cmake` proxy that references
    it only for a dep that can include it."""
    _save_dependencies_cmake(root)
    ensure_mama_cmake(root)


def _get_compile_commands_path(dep: BuildDependency):
    src_build_cmds = f'{dep.src_dir}/build/compile_commands.json'
    bin_build_cmds = f'{dep.build_dir}/compile_commands.json'

    src_exists = os.path.exists(src_build_cmds)
    bin_exists = os.path.exists(bin_build_cmds)

    # choose the latest one
    if src_exists and bin_exists and os.path.getmtime(src_build_cmds) > os.path.getmtime(bin_build_cmds):
        # a src_dir path uses the `${workspaceFolder}` macro
        return '${workspaceFolder}/build/compile_commands.json'
    if bin_exists:
        if dep.build_dir.startswith(dep.src_dir):
            # remove the src dir prefix and use `${workspaceFolder}/`
            rel_build_dir = f'${{workspaceFolder}}{dep.build_dir[len(dep.src_dir):]}/compile_commands.json'
            return rel_build_dir
        return bin_build_cmds # absolute path for a build dir outside src
    return None


_COMPILER_TAGS = ('clang', 'gcc', 'msvc')


def _find_matching_platform_config(dep: BuildDependency, configurations):
    config_name = dep.config.name()
    config_arch = dep.config.arch
    compiler = 'clang' if dep.config.clang else ('gcc' if dep.config.gcc else '')

    def compiler_ok(name):
        """Never repoint a config named for another compiler, eg 'Linux GCC' during a clang build."""
        return not any(tag in name and tag != compiler for tag in _COMPILER_TAGS)

    def match_text(conf):
        """intelliSenseMode ('linux-gcc-x64') is structured and carries the arch. name is free text."""
        return f'{conf.get("intelliSenseMode", "")} {conf["name"]}'.lower()

    # most specific first: platform+arch+compiler down to platform alone, skipping foreign-compiler configs
    for require_compiler, require_arch in ((True, True), (True, False), (False, True), (False, False)):
        for conf in configurations:
            name = match_text(conf)
            if config_name not in name or not compiler_ok(name):
                continue
            if require_arch and config_arch not in name:
                continue
            if require_compiler and (not compiler or compiler not in name):
                continue
            return conf
    return None


def _save_vscode_compile_commands(dep: BuildDependency):
    if not dep.src_dir: # for artifactory pkgs, there is no src_dir
        return
    if not dep.is_root:
        return
    # sanitizer/coverage builds are temporary diagnostics: do not repoint the IDE away from the canonical build
    if dep.config.sanitize or dep.config.coverage:
        return

    cpp_props_path = f'{dep.src_dir}/.vscode/c_cpp_properties.json'
    if not os.path.exists(cpp_props_path):
        return

    commands_path = _get_compile_commands_path(dep)
    if not commands_path:
        return

    # link the compile_commands.json path into c_cpp_properties.json
    cpp_props_text = read_text_from(cpp_props_path)
    import json
    props = json.loads(cpp_props_text)
    configurations = props["configurations"]

    platform_config = _find_matching_platform_config(dep, configurations)

    if not platform_config and len(configurations) > 0:
        platform_config = configurations[0].copy()
        platform_config['name'] = f'{dep.config.name()} {dep.config.arch}'
        configurations.append(platform_config)

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
    text = \
f'''
# Package {name}
set({name}_INCLUDES {includes})
# only {name} libs
set({name}_LIB {own_libs_list})
# includes {name} libs and all dependency libs
set({name}_LIBS {all_libs_list})
'''
    # A dep with no modules emits nothing, so an upgrade reconfigures nothing. A module under no
    # exported include dir is dropped: cmake refuses a FILES entry with no base dir.
    modules = package.exported_modules_with_base(dep.target)
    if modules:
        text += f'''# C++20 module sources a consumer compiles itself
set({name}_MODULES {_get_cmake_path_list(modules)})
set({name}_MODULES_BASE_DIRS {_get_cmake_path_list(package.module_base_dirs(dep.target))})
'''
    return f'${{{name}_INCLUDES}}', text


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
    module_refs = [f'${{{root.name}_MODULES}}'] if package.exported_modules_with_base(root.target) else []
    module_bases = list(package.module_base_dirs(root.target))
    text += package_text

    root.flattened_deps = _get_flattened_deps(root)
    for dep in root.flattened_deps:
        includes_def, package_text = _get_dependency_cmake_defines(dep)
        includes_defs.append(includes_def)
        if package.exported_modules_with_base(dep.target):
            module_refs.append(f'${{{dep.name}_MODULES}}')
            module_bases += package.module_base_dirs(dep.target)
        text += package_text

    includes = ' '.join(includes_defs)
    libs = f'${{{root.name}_LIBS}}' # use the root package to get the full flat list of deps
    text += \
f'''
set(MAMA_INCLUDES ${{MAMA_INCLUDES}} {includes})
set(MAMA_LIBS     ${{MAMA_LIBS}}     {libs})
'''
    if module_refs:
        # literal paths: cmake refuses a file set whose base dirs contain each other
        bases = _get_cmake_path_list(package.drop_nested_dirs(module_bases))
        text += \
f'''set(MAMA_MODULES           ${{MAMA_MODULES}}           {' '.join(module_refs)})
set(MAMA_MODULES_BASE_DIRS ${{MAMA_MODULES_BASE_DIRS}} {bases})
'''

    save_file_if_contents_changed(outfile, text)


def _save_mama_cmake(root: BuildDependency, path: str):
    """One `mama.cmake` proxy, at the path an `include()` named. Generated from the platform registry,
    so it cannot drift from the build dir names that BuildConfig itself uses. A file already at that path
    keeps its contents unless it carries `MAMA_CMAKE_HEADER`. Only cmake knows which `include()` runs,
    so a scan that names one path too many must destroy nothing."""
    config:BuildConfig = root.config
    ninja_version = config.ninja_version()

    def build_dir_defines(build_dir):
        # verbose include directives, because CLion often fails to detect macro paths
        build_dir = build_names.build_dir_name(config, platform_dir=build_dir)
        return f'set(MAMA_BUILD "{build_dir}")\n        include("{root.dep_dir}/{build_dir}/mama-dependencies.cmake")'

    first = (read_lines_from(path, errors='replace') or [''])[0]
    if first and not first.startswith(MAMA_CMAKE_HEADER):
        warning(f'{root.name}: kept the hand-written {path}. Delete it to let mama write the proxy.')
        return
    save_file_if_contents_changed(path, mama_cmake_text(build_dir_defines, ninja_version))


def load_dependency_chain(root: BuildDependency, display=None):
    """
    Main entry point: load the whole dependency chain. Parallel load is the default, and `serial` opts
    out. load() and add_child are thread-safe. Parents block on child futures while they hold a worker
    slot. The default bounded ThreadPoolExecutor can starve on a deep tree, so max_workers is high.
    A semaphore inside Git.run_git caps the SSH-multiplexed fetch concurrency at 8, whatever
    `parallel_max` asks for (see ssh_multiplex.init_fetch_semaphore).
    root: the root BuildDependency, whose loads discover the rest of the graph
    display: the live region of the load phase, or None to print each load as a plain line
    """
    if not root.config.serial_load:
        root.config.parallel_load = True

    ssh_multiplex.init_fetch_semaphore(root.config.parallel_max)

    root.config.update_stats.start()
    claims: dict = {}  # id(dep) -> Event, set once the owner finished that dep and its whole subtree
    claims_lock = threading.Lock()

    def claim_load(dep) -> tuple:
        """(True, event) for the one thread that owns this dep, (False, event) for every later arrival.
        The lock covers the claim alone. The load runs outside it, so different deps stay concurrent."""
        with claims_lock:
            done = claims.get(id(dep))
            if done is not None: return False, done
            claims[id(dep)] = done = threading.Event()
            return True, done

    with concurrent.futures.ThreadPoolExecutor(max_workers=256) as e:
        def load_dependency(dep: BuildDependency, is_entry=False):
            """Load `dep` and its subtree exactly once. A parent that reaches a dep an earlier walk
            finished returns at once. A parent that reaches a dep another parent owns waits for the answer
            it needs. The first parent to reach a dep owns its load, its subtree and its display line."""
            abort.check()  # a queued load must not start a clone during a build abort
            # The walk always enters the dep it starts from, because mamabuild loads the root first and a
            # reload revives deps below a loaded scope. Any OTHER loaded dep is one a sibling already walked.
            if dep.already_loaded and not is_entry:
                return dep.should_rebuild
            owner, done = claim_load(dep)
            if not owner:
                done.wait()  # the owner decides should_rebuild, and the after_load of this parent reads it
                return dep.should_rebuild
            try:
                if display is not None:  # one live line per dep, the shape the build phase already draws
                    _run_phase(display, dep, 'load', lambda s: dep.load(), None, final=True)
                else:
                    dep.load()
                changed = dep.should_rebuild  # what load() returned, and what a replayed load still holds
                if dep.config.parallel_load:
                    futures = [e.submit(load_dependency, child) for child in dep.get_children()]
                    for f in futures:
                        changed |= f.result()
                else:
                    for child in dep.get_children():
                        changed |= load_dependency(child)

                dep.after_load()
                return changed
            finally:
                done.set()  # release every waiter, a failed load included, so no parent hangs
        try:
            load_dependency(root, is_entry=True)
        except BuildError as err:  # a bad url or a dropped clone: the report is complete, a traceback buries it
            _report_error(err, root.config.verbose)
            # Stop the other loads before the exit: the pool's shutdown(wait=True) would otherwise run the
            # ENTIRE queued backlog, minutes of clones for a failed build. After the stop each queued load returns at once.
            SubProcess.terminate_all('load failed')  # the report above already names the target
            exit(-1)
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
        if dep.config.verbose:
            console(f'  - Execute Tasks: {dep.name}', color=Color.BLUE)

        # a dep must not execute twice
        if dep.already_executed:
            error(f"Critical Error: '{dep.name}' executed by child project")
            raise RuntimeError(f"Cyclical Dependency detected for '{dep.name}'")

        # every child dep must have executed first
        for c in dep.get_children():
            if not c.already_executed:
                error(f"Critical Error: child '{c.name}' has not been executed before executing target '{dep.name}'")
                raise RuntimeError(f"Child target not executed before target which requires it: {c.name}")

        _save_cmake_files(dep)
        dep.target._execute_tasks()

        # link compile_commands.json into .vscode/c_cpp_properties.json
        _save_vscode_compile_commands(dep)

        if dep.config.verbose and not dep.config.test:
            if dep.is_root_or_config_target():
                print_dependencies(dep)
            # TODO: different output for non-root targets


def _make_display(config):
    """A live display for one phase of the run. The log belongs to the run, so this reads the open log
    and never opens one of its own. mamabuild opens it after the root load, see open_run_log."""
    import sys, shutil, time
    from .utils.build_display import BuildDisplay
    from .utils.log_writer import get_build_log
    isatty = not system.is_headless()  # a CI runner with a pty still must not get cursor-up escapes
    return BuildDisplay(sys.stdout, isatty=isatty, clock=time.monotonic,
                        term_size=lambda: tuple(shutil.get_terminal_size((100, 24))),
                        verbose=config.verbose, log=get_build_log(), platform=config.name())


# Shared by the two parallel runners (execute_task_chain_parallel, execute_unified).
def _phase_label(dep, kind) -> str:
    # 'load' opens optimistically (clone when no tree exists, else check), and _run_phase relabels it to what load() did
    if kind != 'load': return kind
    if dep.dep_source.is_src: return 'local'  # a local dir clones nothing, and CI reads that opening label
    return 'clone' if not dep.is_real_clone() else 'check'


def _run_phase(display, dep, kind, body, build_slot, detail='', final=False):
    """Run one scheduler phase for `dep` on its single name-keyed display task, routing this thread's
    console output, subprocess CPU and build barrier into it. `final=True` (the build) commits the merged summary."""
    # gate every transition before the display task opens: a stopping build starts no phase and marks no unrun phase failed
    abort.check()
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
        pt = dep.phase_times  # accumulate for the `buildstats` breakdown
        if pt is not None: pt[kind] = pt.get(kind, 0.0) + (time.monotonic() - t0)
        if kind == 'load':
            display.relabel(tid, dep.load_action)  # reflect what load() actually did
            display.set_note(tid, dep.artifactory_archive)  # name the package the exports came from
        display.finish_task(tid, ok, final)


def _configure_body(dep, sink):
    _save_cmake_files(dep)  # children built -> their exports are ready
    dep.target.configure_phase(out=sink)


def _build_body(dep, sink):
    dep.target.build_phase(out=sink)
    dep.already_executed = True
    _save_vscode_compile_commands(dep)


def _stable_cpu_sampler(measure, clock, window=0.5):
    """Gate `measure()` (CPU% since its last call) to re-samples at least `window` seconds apart, and cache
    between. Over the scheduler's irregular sub-second gaps cpu_percent reads a meaningless 0% or 100%."""
    state = {'t': clock(), 'val': 0.0}
    def sample():
        now = clock()
        if now - state['t'] >= window:
            state['val'] = measure(); state['t'] = now
        return state['val']
    return sample


# Overprovisioning: max reserved cores = core_budget * this. MSBuild tolerates 2x, but on Linux make already
# saturates the cores. _GB_PER_COMPILE is the peak RSS of a heavy C++ TU and caps the parallel compiles by total RAM.
_OVERPROVISION_WIN, _OVERPROVISION_UNIX = 2.0, 1.0
_GB_PER_COMPILE = 1.5


def _mem_capped_budget(jobs: int) -> int:
    """Cap the core budget by RAM so parallel heavy C++ compiles cannot OOM. Never below 1 or above `jobs`."""
    import psutil
    gb = psutil.virtual_memory().total / (1024 ** 3)
    return max(1, min(jobs, int(gb / _GB_PER_COMPILE)))


def _make_scheduler(config, **extra):
    """The build Scheduler with a stable psutil CPU sampler and the Ctrl+C child-killer."""
    import psutil, time
    from .build_scheduler import Scheduler
    cpu = system.usable_cpu_count()
    psutil.cpu_percent(interval=None)  # prime the sampler (first call always returns 0.0)
    win = system.System.windows
    budget = config.jobs if win else _mem_capped_budget(config.jobs)  # Linux: avoid OOM on parallel C++ compiles
    extra.setdefault('overprovision', _OVERPROVISION_WIN if win else _OVERPROVISION_UNIX)
    return Scheduler(max_configure=min(cpu * 2, 32), core_budget=budget, abort_hook=SubProcess.terminate_all,
                     cpu_sampler=_stable_cpu_sampler(lambda: psutil.cpu_percent(interval=None), time.monotonic),
                     **extra)


def _handle_failure(display, failed):
    """Replay the first failed job's captured output plus the reason, then RETURN so the caller still
    prints the aggregate diagnostics before the nonzero exit. A Ctrl+C abort exits at once."""
    if isinstance(failed.error, KeyboardInterrupt):
        console(f'  [BUILD INTERRUPTED]  {failed.error}', color=Color.RED)
        exit(-1)
    console(f'  [BUILD FAILED]  {failed.node.name}', color=Color.RED)
    if display.isatty:  # non-TTY already dumped the output on finish
        display.replay(failed.node.name)
    if failed.error: _report_error(failed.error, failed.node.config.verbose)


def _report_error(err: BaseException, verbose: bool):
    """A BuildError means the user's build or url broke: print its report alone, because a traceback would
    bury it under mama's own call stack. Anything else keeps the traceback (BuildError too under verbose)."""
    import traceback
    if isinstance(err, abort.BuildAborted):
        error(f'  {err}'); return  # a stopped job, not a failure: the FIRST failure printed the reason
    if isinstance(err, BuildError):
        error(f'  {err}')
        if not verbose: return
    console(''.join(traceback.format_exception(type(err), err, err.__traceback__)))


def _deploy_run_postpass(deps, config):
    """Deploy/run/test post-pass: target-specific, cheap, and it stays serial and children-first."""
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
    root = next((d for d in deps if d.is_root), deps[-1])
    display = _make_display(config)
    sched = _make_scheduler(config, pending_log=display.set_pending)
    cfg = lambda d: _run_phase(display, d, 'configure', lambda s: _configure_body(d, s), sched.build_slot)
    bld = lambda d: _run_phase(display, d, 'build', lambda s: _build_body(d, s), sched.build_slot,
                               _build_detail(d), final=True)  # build is the dep's last phase -> commit its summary
    jobs = build_dep_jobs(deps, cfg, bld, weight_fn=_reserve_weight)  # sets critical-path (trunk) priorities
    system.set_active_display(display)
    start = time.monotonic()
    with _build_insights_session(config, root):  # MSVC buildstats: wrap the build in a vcperf trace (else no-op)
        try:
            failed = sched.run(jobs)
        finally:
            display.close()
            system.set_active_display(None)
            SubProcess.clear_abort()  # re-arm spawning (run() returned -> all workers drained)
    if failed is not None:
        _handle_failure(display, failed)      # replay the failed target + traceback (scroll up for detail)
        _print_diagnostics(display, deps, failed.node.name); exit(-1)  # ...then the aggregate summary last
    _print_build_summary(deps, time.monotonic() - start)
    _print_diagnostics(display, deps)
    if config.buildstats:
        print_buildstats(deps)
        _print_build_insights(config, deps)
    _deploy_run_postpass(flat_deps_reverse, config)


def _reserve_weight(dep) -> int:
    """Cores reserved for a build job AT LAUNCH. The ungated root and a custom build(), which reserves
    from inside cmake_build() (the barrier), launch free (0). A default build reserves its capped cores."""
    if dep.is_root or dep.target._has_custom_build(): return 0
    return dep.target._reserved_cores()


def _build_detail(dep) -> str:
    cores = dep.config.jobs if dep.is_root else dep.target._reserved_cores()  # root runs alone at full -j
    return f'J{cores:<2}'


def _node_marker(dep) -> str:
    """[R]oot / [L]eaf (no deps of its own) / [T]runk (has deps) - quick visual of tree position."""
    if dep.is_root: return '[R]'
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
        reserve = t._reserved_cores()  # canonical reserve (== actual -j)
        flags = []
        if t._has_custom_build(): flags.append('custom-build')   # -> configure skips probe -> -j=config.jobs
        if d.nothing_to_build: flags.append('nothing_to_build')
        if d.from_artifactory: flags.append('artifactory')
        console(f'  {d.name:<22}{tu:>6}  {via:<16}{probe:>6}{reserve:>9}{probe:>5}   {" ".join(flags)}')


def _print_build_summary(deps, elapsed: float):
    """End-of-session line: how many targets actually compiled (cached/artifactory ones excluded)."""
    built = sum(1 for d in deps if d.should_rebuild and not d.from_artifactory and not d.nothing_to_build)
    console(f'Built {built} target(s) in {get_time_str(elapsed)}', color=Color.GREEN)
    # The per-target deploy lines print inside the build job, so they reach the log and never the screen.
    deployed = deps[0].config.deploy_stats.summary_line() if deps else ''
    if deployed: console(deployed, color=Color.GREEN)
    _warn_on_mixed_build_types(deps)


def _dep_build_type(dep) -> str:
    """`debug`, `release` or '': what the artifacts of this dep are. A fetched package has no cmake
    cache of its own, so the `O` record of its papa.txt answers instead."""
    from .papa_deploy import PapaFileInfo  # local import: avoid a cycle
    recorded = build_names.build_dir_build_type(dep)
    if recorded: return recorded
    # A summary must never fail a build that already succeeded, so a missing or malformed file answers ''.
    try: attributes = PapaFileInfo(path_join(dep.build_dir, 'papa.txt')).attributes
    except Exception: return ''
    return next((a for a in attributes if a in ('debug', 'release')), '')


def _warn_on_mixed_build_types(deps):
    """Name every package built in another build type than this run.

    The mix is allowed on purpose. One target in debug reads a stack trace, and a rebuild of the whole
    tree to get it costs more than that."""
    config = deps[0].config if deps else None
    if not config or not config.print: return
    wanted = 'debug' if config.debug else 'release'
    others = [(d.name, t) for d in deps for t in (_dep_build_type(d),) if t and t != wanted]
    if not others: return
    warning(f'  This build is {wanted}, and {len(others)} package(s) hold another build type:')
    for name, kind in others[:_MIXED_TYPE_LIMIT]:
        warning(f'    {name: <22} {kind}')
    if len(others) > _MIXED_TYPE_LIMIT: warning(f'    ... (+{len(others) - _MIXED_TYPE_LIMIT} more)')


_DIAG_LIMIT = 8  # compiler warnings/errors surfaced per target in the post-build summary
_MIXED_TYPE_LIMIT = 8  # packages named in the mixed build-type warning


def _print_diagnostics(display, deps, failed_name=None):
    """Post-build: show the compiler warnings/errors the live display hid on a successful build, up to
    _DIAG_LIMIT per target, errors first. `failed_name` (the target that broke the build) sorts FIRST,
    so the warnings of unrelated siblings do not bury it."""
    ordered = sorted(deps, key=lambda d: d.name != failed_name) if failed_name else deps
    printed = False
    for dep in ordered:
        diags, n_err, n_warn = display.diagnostics(dep.name, _DIAG_LIMIT)
        if not diags: continue
        if not printed: console('\n  Compiler diagnostics:', color=Color.BLUE); printed = True
        counts = ', '.join(f'{n} {w}(s)' for n, w in ((n_err, 'error'), (n_warn, 'warning')) if n)
        console(f'  {dep.name}: {counts}' + ('   <-- BUILD FAILED HERE' if dep.name == failed_name else ''))
        for sev, text in diags:  # a cmake block spans lines, so keep its body indented under the header
            (error if sev == 'error' else warning)(f'    {text}'.replace('\n', '\n      '))
        if n_err + n_warn > len(diags): console(f'    ... (+{n_err + n_warn - len(diags)} more)')


# buildstats (stage 1): a normalized horizontal bar per package, segmented load/configure/build.
_BAR_FILL = 40  # the slowest package fills this width, the rest scale proportionally
_BUILDSTATS_FLOOR = 0.33  # omit packages faster than this - they are noise on the chart
_BAR = (('load', Color.BLUE), ('configure', Color.MAGENTA), ('build', Color.GREEN))
_GLYPHS_SHADE = ('░', '▒', '▓')  # light/medium/dark blocks (UTF-8 terminals)
_GLYPHS_ASCII = ('-', '=', '#')  # legacy code-page fallback (Windows cp1252 cannot encode the blocks)
_glyphs_cache = None


def _can_encode_blocks(encoding) -> bool:
    try: ''.join(_GLYPHS_SHADE).encode(encoding); return True
    except (UnicodeEncodeError, LookupError): return False


def _bar_glyphs():
    """Block shades on a UTF-8 terminal, ASCII on a legacy code page. Decided ONCE: the output
    encoding is constant per process, so a re-test per report or per row is waste."""
    global _glyphs_cache
    if _glyphs_cache is None:
        _glyphs_cache = _GLYPHS_SHADE if _can_encode_blocks(getattr(sys.stdout, 'encoding', None) or 'ascii') \
                        else _GLYPHS_ASCII
    return _glyphs_cache


def _buildstats_bar(times: dict, total: float, max_total: float, glyphs) -> str:
    """A bar whose length scales with total/max_total. Inside it, load/configure/build take shares of
    that length (shaded, colored). Right-padded to full width so the trailing total aligns across rows."""
    bar_len = max(1, round(total / max_total * _BAR_FILL)) if max_total > 0 else 0
    out, used, last = [], 0, len(_BAR) - 1
    for i, ((kind, color), ch) in enumerate(zip(_BAR, glyphs)):
        n = (bar_len - used) if i == last else min(round(times.get(kind, 0.0) / total * bar_len), bar_len - used)
        if n > 0: out.append(get_colored_text(ch * n, color))
        used += n
    return ''.join(out) + ' ' * (_BAR_FILL - bar_len)


def print_buildstats(deps):
    """`buildstats`: one normalized bar per package (load / configure / build), slowest first, with its
    total wall time. The chart omits packages faster than _BUILDSTATS_FLOOR seconds, so it stays relevant."""
    label = 'Build times'
    rows = []
    for d in deps:
        pt = d.phase_times
        if not pt: continue
        total = sum(pt.values())  # once per dep, not recomputed in a filter
        if total >= _BUILDSTATS_FLOOR: rows.append((d.name, pt, total))
    if not rows: return
    rows.sort(key=lambda r: r[2], reverse=True)
    max_total = rows[0][2]
    name_w = max(min(max(len(name) for name, _, _ in rows), 24), len(label))  # fit the names AND the header label
    glyphs = _bar_glyphs()
    legend = '  '.join(get_colored_text(f'{ch} {kind}', color) for (kind, color), ch in zip(_BAR, glyphs))
    console(f'\n  {label:<{name_w}}  {legend}')  # label padded to the name column so the legend sits over the bars
    for name, pt, total in rows:
        console(f'  {name:<{name_w}.{name_w}}  {_buildstats_bar(pt, total, max_total, glyphs)}  {get_time_str(total)}')


def _build_insights_session(config, root: BuildDependency):
    """MSVC `buildstats`: a live vcperf /timetrace session wrapping the build. Linux `buildstats`: record
    the start time so the report collects only this run's clang -ftime-trace JSONs. Else a null context."""
    import contextlib, time
    if not config.buildstats:
        return contextlib.nullcontext()
    if config.msvc:
        from .build_insights import find_vcperf, VcPerfSession, timetrace_path
        vcperf = find_vcperf(config)
        if not vcperf:
            warning('buildstats: vcperf.exe not found (set VCPERF= or run from a Developer Command Prompt);'
                    ' skipping MSVC Build Insights')
            return contextlib.nullcontext()
        config._timetrace_json = timetrace_path(root.build_dir)
        return VcPerfSession(vcperf, config._timetrace_json)
    config._buildstats_start = time.time()  # wall start: post-build we analyze only traces newer than this
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
    elif config._buildstats_start is not None:
        _print_clang_insights(config, deps, label, dep)


def _print_msvc_insights(config, label, dep):
    path = config._timetrace_json
    if not path or not os.path.exists(path): return
    import json
    from .build_insights import parse_timetrace, print_buildstats_deep
    scope_paths = [p for p in (dep.src_dir, dep.build_dir) if p] if dep else None
    try:
        with open(path, encoding='utf-8') as f: data = json.load(f)
        stats = parse_timetrace(data, scope_paths)
    except Exception as e:
        warning(f'buildstats: failed to read vcperf trace: {e}'); return
    print_buildstats_deep(stats, label)


def _print_clang_insights(config, deps, label, dep):
    import time
    from .build_insights import collect_clang_traces, parse_clang_traces, print_buildstats_deep
    if not config.clang:
        warning('buildstats: deep per-file insights need Clang -ftime-trace; build with `clang` for the breakdown')
        return
    start = config._buildstats_start
    scoped = [dep] if dep else deps  # a <target>: only its build dir, else every package's
    paths = []
    for d in scoped:
        bd = d.build_dir
        if bd: paths += collect_clang_traces(bd, since=start)
    stats = parse_clang_traces(paths, wall_s=time.time() - start)
    print_buildstats_deep(stats, f'{label} (clang)')


def _command_verb(config) -> str:
    """What this run is doing, in the user's terms. rebuild sets build+clean and update sets build,
    so the most specific command wins."""
    if config.rebuild: return 'rebuilding'
    if config.update:  return 'updating'
    if config.clean:   return 'cleaning'
    return 'building'


def _toolchain_name(config) -> str:
    """'clang 18.1 libstdc++' / 'gcc 14.3' - what this run builds with, named from the RESOLVED compiler.
    config.clang/gcc describe the host, so a cross build would misname the compiler of the android NDK.
    '' when unresolved: a banner must never fail a build."""
    try:
        if config.msvc:
            return 'msvc ' + msvc_toolset_version(config.get_msvc_tools_path())
        cc, _, ver = config.get_preferred_compiler_paths()
        cc = os.path.basename(cc)  # basename: only the compiler itself, never a 'gcc-toolchain' dir above it
        name = 'clang' if 'clang' in cc else ('gcc' if 'gcc' in cc else '')
        if not name: return ''
        ver = '.'.join(ver.split('.')[:2]) if ver else ''
        stdlib = f' {config.clang_stdlib}' if (name == 'clang' and config.linux) else ''
        return f'{name}{" " + ver if ver else ""}{stdlib}'
    except Exception:
        return ''


def _platform_name(config) -> str:
    """'android-36 arm64 ndk-29.0.14206865' / 'linux x64' - the TARGET this run builds FOR.
    '' when unresolvable: a banner must never fail a build."""
    try:
        return config.platform.banner_name()
    except Exception:
        return ''


def print_build_banner(config, count=None):
    """One-line preview above the first task line: version, command, target count, platform, toolchain.
    `count` is None on the unified path, whose graph grows as deps load, so the total is unknown."""
    targets = f' {count} target(s)' if count is not None else ''
    platform = _platform_name(config)
    toolchain = _toolchain_name(config)
    console(f'Mama {__version__} {_command_verb(config)}{targets}'
            + (f' {platform}' if platform else '') + (f' with {toolchain}' if toolchain else ''),
            color=Color.GREEN)


def execute_unified(root: BuildDependency, scope: DepsOnlyScope = None):
    """Dynamic DAG scheduler that interleaves clones with configure and build: each completed LOAD grows
    the graph with its children's jobs, and a CONFIGURE waits on its own LOAD plus its children's BUILDs,
    so leaves build while deeper deps still clone. A plain full build uses this path, and main() falls
    back to the classic path otherwise. Deploy/run/test stay serial. mamabuild loads the ROOT before it
    calls this, because everything below it needs what its settings() picks. Under a `deps_only`
    DepsOnlyScope every dep still loads, but only the deps the scope includes get a CONFIGURE and a BUILD job."""
    import time
    from .build_scheduler import Job, LOAD, CONFIGURE, BUILD, assign_priorities
    config = root.config
    ssh_multiplex.init_fetch_semaphore(config.parallel_max)
    print_build_banner(config)  # the root load has locked compiler + stdlib
    config.update_stats.start()
    display = _make_display(config)
    sched = _make_scheduler(config, max_load=config.parallel_max, pending_log=display.set_pending)
    load_jobs: dict = {}; cfg_jobs: dict = {}; bld_jobs: dict = {}  # dep -> Job (mutated under sched lock)
    builds = lambda d: scope is None or scope.includes(d)  # a `deps_only` run gives some deps a LOAD only

    def make_jobs(dep, parent_load, parent=None):
        L = Job((dep, 'L'), LOAD, (lambda d=dep: _do_load(d)), deps=({parent_load} if parent_load else set()), node=dep)
        load_jobs[dep] = L
        if scope is not None: scope.enter(dep, parent)
        return [L] + (make_build_jobs(dep) if builds(dep) else [])

    def make_build_jobs(dep):
        """The CONFIGURE + BUILD pair of the dep: configure waits on its own load and on the builds of
        every child known so far, and grow() in _do_load adds the rest as the graph discovers them."""
        C = Job((dep, 'C'), CONFIGURE, (lambda d=dep: _do_configure(d)), node=dep,
                deps={load_jobs[dep], *(bld_jobs[c] for c in dep.get_children() if c in bld_jobs)})
        B = Job((dep, 'B'), BUILD, (lambda d=dep: _do_build(d)), deps={C}, node=dep,
                weight=(lambda d=dep: _reserve_weight(d)), ungated=dep.is_root)
        cfg_jobs[dep] = C; bld_jobs[dep] = B
        return [C, B]

    def _do_load(dep):
        def body(sink):
            dep.load()  # clone + parse mamafile + dependencies() -> populates dep.children (no recursion)
            def grow():  # runs under the scheduler lock: safe to mutate registries + add edges
                new = []
                for child in dep.get_children():
                    if child not in load_jobs:
                        new += make_jobs(child, load_jobs[dep], dep)
                    elif scope is not None and scope.is_inside(dep):  # shared dep, now reached from inside the scope
                        # A load populates its children before it grows the graph, so a promoted dep can name a
                        # child no job knows yet. That child builds when its own parent registers it.
                        for d in scope.promote(child):
                            if d in load_jobs: new += make_build_jobs(d)
                C = cfg_jobs.get(dep)  # absent when the scope excludes this dep
                if C is not None: C.deps.update(bld_jobs[c] for c in dep.get_children() if c in bld_jobs)
                assign_priorities(list(cfg_jobs.values()) + list(bld_jobs.values()))  # re-rank the critical path (trunk)
                return new
            sched.grow(grow)
        # the root's load is a no-op replay, and an excluded dep's load is its final phase, so it commits the summary line
        _run_phase(display, dep, 'load', body, sched.build_slot, final=not builds(dep))

    def _do_configure(d):
        def body(sink):
            d.after_load()  # children have loaded AND built by now: propagate their 'changed' up to this dep
            if scope is not None: scope.prepare(d)
            _configure_body(d, sink)
        _run_phase(display, d, 'configure', body, sched.build_slot)
    def _do_build(d):
        _run_phase(display, d, 'build', lambda s: _build_body(d, s), sched.build_slot, _build_detail(d), final=True)

    system.set_active_display(display)
    start = time.monotonic()
    with _build_insights_session(config, root):  # MSVC buildstats: wrap the build in a vcperf trace (else no-op)
        try:
            failed = sched.run(make_jobs(root, None))
        finally:
            display.close()
            system.set_active_display(None)
            config.update_stats.stop()
            SubProcess.clear_abort()  # re-arm spawning (run() returned -> all workers drained)
    flat = get_flat_deps(root)
    built = [d for d in flat if builds(d)]  # a `deps_only` run reports on its own deps, not the whole tree
    if failed is not None:
        _handle_failure(display, failed)      # replay the failed target + traceback (scroll up for detail)
        _print_diagnostics(display, built, failed.node.name); exit(-1)  # ...then the aggregate summary last
    if scope is not None and not scope.found:
        console(f"ERROR: specified target='{config.target}' not found!"
                f" Available targets: {', '.join(sorted(d.name for d in flat))}")
        exit(-1)
    _print_build_summary(built, time.monotonic() - start)
    _print_diagnostics(display, built)
    if config.buildstats:
        print_buildstats(flat)  # every dep loaded, so show the load bars even for excluded deps
        _print_build_insights(config, built)
    if scope is not None: scope.widen_for_deploy()
    _deploy_run_postpass(reversed(built), config)


def find_dependency(root: BuildDependency, name: str) -> BuildDependency:
    """ Find the root target or a specific command line target by name. """
    if root.name.lower() == name.lower():
        return root
    for dep in root.get_children():
        found = find_dependency(dep, name)
        if found: return found
    return None
