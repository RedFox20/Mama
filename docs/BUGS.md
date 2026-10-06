# Known bugs

Two lists. Open defects first, then the fixed ones. Newest first in both.

Add an entry to **Open** when you find a bug you do not fix in the same change. An open entry carries
the reproduction, the `file:line` of the cause and the shape of the fix. A bug that needs more than
five lines gets its own handover doc, and the entry links to it.

**When you fix an entry, compact it and move it to Closed, in the same commit.** A closed entry is one
bullet of at most two lines: what broke, then `Fix:` and what the code does now. Drop the reproduction,
the `file:line` and the design options. The commit and its tests hold that detail. This list only grows,
so cut every word that a reader of the fix does not need.

## Open

- **A fetched dep prints `BUILD [<child> changed]` and builds nothing.** `after_load` sets `should_rebuild`
  on a dep whose child rebuilt (`build_dependency.py:after_load`), and it guards only a shim. A dep that
  unpacked a package after its clone, or a local dep, gets the flag, but `_build_work_enabled` refuses a
  dep `from_artifactory` (`build_target.py:_build_work_enabled`). Repro: a KrattGCS CI run where ReCpp has
  no package prints `BUILD [ReCpp changed]` for `logging` with `bld 0.0s`. Fix: split "this dep builds"
  from "a dep below changed". A fetched dep must not build, but its parents still need the signal, because
  they compile the headers of the changed dep through it.

- **A targeted build can write a `B` record that does not match what the parent compiled against.** In
  `mama build X`, `_should_build` skips every dep except X, and `mark_unbuilt_target_deps` revives only a
  stale locked commit. So an unlocked dep can keep artifacts older than its checkout. `current_archive_name`
  names the checkout. A header from the source dir matches that name. A header from the build dir matches
  the older `git_status` commit. Fix: when the artifacts of a dep that does not build differ from its
  checkout, write a `B` value that never matches, so a later run builds the parent from source.

- **The root `settings()` reads a build dir that the same `settings()` can then rename.** `BuildTarget.__init__`
  names the root dirs before `settings()` runs (`build_target.py:131`). The guard in `_dep_path` fires only
  on an empty path (`build_target.py:157`), so `build_dir()` returns that early path. A later `prefer_clang()`
  or `enable_threads()` renames the dir. SPEC section 3 step 8 says the call raises, and
  `test_the_root_settings_cannot_read_a_build_dir` passes only because it clears the dirs first. A raise on
  every call breaks a root that reads its CMake cache in `settings()`. Fix: after the lock, raise only when
  `settings()` read a dir whose name then changed.

- **A wasm or MSVC build appends a new entry to `c_cpp_properties.json` on every build.** The compiler
  tag of `_find_matching_platform_config` reads `config.clang` and `config.gcc` (`dependency_chain.py:361`).
  MSVC sets neither, so `compiler_ok()` rejects every entry that names `msvc`, the Windows entry too. A
  wasm run has `config.gcc` set, and the copy of `configurations[0]` keeps its `windows-msvc-x64` mode
  (`dependency_chain.py:411`). So no entry ever matches. Fix: take the tag from
  `platform.compiler_family()`, and drop `intelliSenseMode` and `compilerPath` from the copy.

- **A root that exports its whole source dir also copies the dir tree of its workspace.** The last
  fallback of `default_package_includes` exports `''` (`build_target.py:1497`). `copy_dir` then walks
  `packages/` too, and it makes every dir it enters before the header filter runs (`fileio.py:210`). A
  deploy writes an empty `include/<root>/packages/<dep>/<platform>/CMakeFiles/...` tree, and on Windows
  that path passes 260 chars. Repro: `tests/test_papa_deploy` under a long temp dir. Fix: prune the
  workspace dir in `_prune_walk_dirs`, and make a dir only when a file in it passes the filter.

- **The RAM cap for parallel compiles reads the host memory.** `_mem_capped_budget` divides
  `psutil.virtual_memory().total` by `_GB_PER_COMPILE` (`dependency_chain.py:696`), and psutil reads
  `/proc/meminfo`, which reports the host inside a memory-limited cgroup. A container held to 2 GB on a
  64 GB host then allows 42 heavy compiles. Fix: read `memory.max` (v2) or
  `memory/memory.limit_in_bytes` (v1) beside `_cgroup_cpu_quota`, and take the smaller.

- **The module strip runs the target-wide matcher against every exported archive.**
  `export_stripped_module_libs` loops `target.exported_libs` and calls `_module_object_members(target, src)`
  for each (`package.py:405`), which knows no archive-to-module ownership. A target that exports both its
  own `libcore.a` and a prebuilt `libvendor.a` loses a vendor member named `api.cppm.o` when the target
  exports any `api.cppm`. `_ambiguous_names` cannot protect it: it scans the source and build trees, and
  the vendor sources are in neither. Repro: export two static libs, put `api.cppm.o` in the prebuilt one,
  export `api.cppm` from the other. Fix: record which archive compiled each module. Do NOT skip an
  archive no scanned tree accounts for: an intermediary archive holds the module objects of a package
  below it, whose source is in another tree by design, and that skip breaks every chain build.

- **A targeted run leaves an out-of-scope root without its `mama.cmake` proxy.** A targeted run narrows
  `flat_deps` to the subtree of the target (`main.py:428-431`), so `_save_cmake_files` never runs for a
  dep outside it. Mama never configures that dep either, so no mama run breaks. A user who then runs
  plain cmake in that dir, or opens it in an IDE, finds no proxy and an empty `MAMA_INCLUDES`. Fix: write
  the proxy for every loaded dep whose `CMakeLists.txt` includes it, whatever the scope of the run.

- **A cross build whose root mamafile calls `prefer_clang()` bootstraps a host tool on every lookup.**
  `prefer_clang` returns early when the target is not Linux, so the parent predicts `linux` while the
  Linux child writes `linux-clang`. The tool is found after each bootstrap, but the predicted path never
  appears, so the next run spawns the child again and `auto_build=False` answers None. Fix: let the child
  report the dir it wrote, and read that report before predicting.

- **A Windows abort can still miss a process spawned late in the grace window.** `terminate_all` reads
  each tree before the signal and once more after it, so a child that spawns a process seconds later and
  then exits leaves it with no live root to walk from. Reproduce with a child that sleeps 2 seconds,
  spawns a grandchild, and exits before the 30 second grace ends. Fix: create every Windows child inside
  a job object and terminate the job, which takes every descendant whatever its start time.

## Closed

- **Under `deps_only`, a shared dep promoted into the scope could configure before its children built.**
  Fix: the scheduler adds the child edges after every promoted dep has its jobs.

- **An unpublish stopped at the first dep that never published.** The server answers `550` for a dir it
  does not have. Fix: a `550` listing reads as no archive.

- **`mama dirty` missed a dependent that reached the target through another one, or had no source.** Fix:
  ask each dep whether its subtree holds the target, and load the whole graph with no deferred dep.

- **A local module source dir named like the build dir, eg `android`, dropped out of its version.** One
  module got a version per platform. Fix: skip the workspace dir only, which holds every build dir.

- **The root named, created and cleaned its build dir before its `settings()` ran.** Fix: `settings()` runs
  first, so a root `prefer_clang()` leaves no stray dir, and a clean takes the dir that `settings()` names.

- **A non-root mamafile could call `config.wasm.enable_threads()` and split the tree across two dirs.** Fix:
  a call after the root `settings()` changes nothing and prints a warning.

- **A C file got no mamafile C flag and none of the flags mama adds, eg a sanitizer or `-march`.**
  Fix: `CMAKE_C_FLAGS` carries them. An `add_cmake_options()` value of either flags variable goes first.

- **`run_gdb` checked the quoted exe path, so a program under a dir with a space raised `Could not find`.**
  Fix: it checks the path without the quotes.

- **Sanitizer and coverage flags followed the host compiler flags on a cross platform.** Fix: each platform
  declares its compiler family, and only a position independent platform gets `-fPIE` and `-pie`.

- **A GNU cross sysroot reached only the C++ flags, so the CMake C probe linked against the host root.**
  Fix: `GnuCross` puts the sysroot in its `Toolchain`, and cmake gets `CMAKE_SYSROOT`.

- **On Windows, `read_text_from` used the locale codec, so a non-ASCII path rewrote `mama.cmake` each run.**
  Fix: it reads UTF-8, the codec of `write_text_to`.

- **On Windows, the drive case of the working dir reached `mama.cmake`, so each case rewrote it.**
  Fix: `normalized_path` writes the drive letter in upper case.

- **Each build wrote the `mama-nomodules/` copy again, so every consumer linked again.**
  Fix: a copy stays while its archive and the module declarations do not change.

- **Parallel CI uploads wrote env credentials to one keyring file and corrupted it.** Fix: only typed
  credentials reach the keyring, and a keyring file that does not parse moves to `<file>.corrupt`.

- **`export_libs('.')` kept a stale root lib or a `Debug/` lib from a multi-config build dir.**
  Fix: a lib under `<cmake_build_type>/` wins the basename dedup.

- **An x86 MSVC target under Ninja or Unix Makefiles got `CMAKE_GENERATOR_TOOLSET=host=x86`.**
  Fix: `platform_opts` emits the toolset only under Visual Studio, the same as `-A`.

- **An MSVC target under Unix Makefiles got the MSBuild flags `/maxcpucount`, `/v:m` and `/nologo`.**
  Fix: `_mp_flags` and `_buildsys_flags` test `enable_unix_make` before `config.msvc`.

- **A local module never hit its artifactory package.** The upload hashed the `mama.cmake` the build
  wrote, and the download did not. Fix: the version walk skips every file named `mama.cmake`.

- **The generator error recovery was dead code on Windows, and it kept `CMakeFiles`.** Fix: the
  generator check comes first, and the recovery wipes `CMakeCache.txt` and `CMakeFiles`. A dir with no
  fingerprint compares its cached generator before the configure.

- **ls-remote gave up after 5 seconds, and a failure under `update` dropped the package of a shim.**
  Fix: it waits `git_timeout`, and a probe that resolves no commit loads the cached package.

- **A TLS failure marked the network unavailable, so the run skipped every later fetch and clone.**
  Fix: `is_network_error` answers False for an `ssl.SSLError`, bare or wrapped in a `URLError`.

- **The default job count read the host cpus, so a container build started far too many compilers.**
  Fix: `usable_cpu_count` caps it by the cgroup cpu quota and the cpuset affinity mask.

- **Only the root CMakeLists.txt named the proxy, so an `include()` in a subdirectory got none.**
  Fix: the scan follows `add_subdirectory()`, and every file it reads contributes its own includes.

- **`enable_cxx26()` wrote `c++2b` for GCC and Clang, which is C++23.** Fix: write `c++2c`.

- **An intermediary archive kept the module objects of the packages below.** Fix: the strip walks them all.

- **A module no exported include dir held vanished silently.** Fix: packaging warns before the filters run.

- **Compiler discovery split PATH on `:`, cutting the drive letter off Windows entries.**
  Fix: use the platform separator.

- **Compiler discovery named a compiler the host lacks: a link and its target carry different suffixes.**
  Fix: keep the spelling that exists at the resolved root.

- **The release-CRT enforcement never reached a project on cmake 3.15 or later.** Policy CMP0091 moved
  the flag, and `mama.cmake` reaches no third-party project. Fix: set it on the configure command line.

- **A seeded configure dropped every binutil, so `ar` and `ranlib` reached the build empty.**
  Fix: the seed replays a closed set of cache keys.

- **The module strip asked for a bare `ar`, which the Android NDK lacks.** Fix: it answers `llvm-ar`.

- **A copy of a thin archive lost every member, which it names by path.** Fix: the strip leaves it alone.

- **A cached ninja version outlived its executable, so an upgrade never reached the guard.**
  Fix: probe on every configure.

- **The module strip deleted an object no exported module named, because a bare name matched anything.**
  Fix: a member goes to the module sharing the most path.

- **A casing variant dropped a module on macOS: the path compare merged case on Windows alone.**
  Fix: `match_path` follows the filesystem.

- **The second build published the archive of the first: the strip read its own recorded copy.**
  Fix: it reads the archive the build wrote.

- **An empty compiler floor left the cmake `if` with no right side.** Fix: an empty floor refuses.

- **The Windows strip found neither `lib.exe` nor its archive members.**
  Fix: read the MSVC toolset, and match the full path it lists.

- **A source-built dependency exported the archive that still held its module objects.**
  Fix: `exported_libs` points at a copy under `mama-nomodules/`.

- **The module strip removed every definition the interface unit compiled.** Fix: the API states an
  exported module defines only its interface, and every strip warns.

- **One lowered compiler floor enabled every module package.** Fix: one floor per compiler family.

- **A recursive package shipped a child module twice, so a consumer declared it twice.**
  Fix: write `M` records for the deployed target alone.

- **A `PUBLIC` module file set broke a consumer that installs and exports its own target.**
  Fix: `mama_target_modules(target [scope])` takes a scope.

- **A Windows abort left a grandchild running.** The tree sweep walked from a child that had already
  exited. Fix: read the descendant pids before the signal, and kill each orphan after it.

- **`host_build_dir` named a dir the bootstrap child never wrote.** A compiler, a dep arg or an arm64
  host each moved the child elsewhere. Fix: name it by the rules the child follows, search every host
  build dir on a miss, and treat only a runnable arch as native.

- **A bare target name never reached an unpublish.** The constructor froze `user_target` before the
  deduction ran. Fix: freeze it inside the deduction.

- **A multi-config generator offered three configurations that cannot link.** Only `CMAKE_BUILD_TYPE`
  reached cmake. Fix: pass `CMAKE_CONFIGURATION_TYPES`, the type of this run first.

- **An `-march` pin renamed every build dir and broke every consumer of that path.** Fix: the pin
  renames the artifactory archive alone.

- **A shim of another platform kept serving a deleted package.** The purge read one build dir. Fix:
  walk every build dir of the dep, and drop each marker that names a deleted archive.

- **A `clean` run never unpublished, and said nothing.** `clean_only` returns before the usual call
  site. Fix: unpublish on that path too, and keep the cached zips outside the build dir.

- **`test_failure_fires_abort_hook_once_to_kill_in_flight` failed once and never again.** 26 suite runs
  and 7300 iterations reproduced nothing. Fix: split the assert, so a repeat names the half that broke.

- **A Windows test moved a `.git` dir that git had just written.** A rename refuses while a handle on
  the tree is open. Fix: `git init --separate-git-dir` writes that shape, so nothing moves.

- **An offline `mama update` ended the run over a package it had cached.** `update` skips the cached zip
  for a fresher copy, and nothing used it when the download answered nothing. Fix: use that zip.

- **`noart` left an `add_artifactory_pkg` dep with no package.** A package dep has no source to build
  instead. Fix: a package dep ignores `noart`, a rebuild and a clean, and refuses a deploy or an upload.

- **A deploy re-rooted an include tree that an archive already unpacked.** `as_includes_root` applied
  its alias to a fetched package too. Fix: copy an unpacked archive as it stands.

- **`mama build debug` after `mama build` built release, and said nothing.** The shared build dir forced
  no configure. Fix: reconfigure on a build-type flip, and name every package that holds the other type.

- **A revived deferred dep with a parent-supplied mamafile crashed the run.** The revive kept the
  children, so `add_child` refused a duplicate. Fix: drop them, and let the registry re-attach them.

- **`mama update` could not move a stale shim forward when no new package answered.** Nothing dropped
  the marker. Fix: drop a marker whose commit upstream has left behind, then clone the source.

- **A root project with no `mamafile.py` wrote its packages into the user home dir.** Only a mamafile
  parse assigned `workspaces_root`. Fix: default it to the root source dir.

- **`default_package()` ran the default packaging only when told not to.** Fix: the `no_includes` and
  `no_libs` guards now read `not`.

- **An artifactory package deploy ignored `includes_filter`.** Mama skipped `package()` for a fetched
  target. Fix: always run it, and keep the recorded exports when the hook cannot read a build product.

- **`test_a_changed_source_never_arms_the_gate` failed about one run in three.** An edit inside one
  mtime tick reads as no change. Fix: wait 10 ms before the edit.
