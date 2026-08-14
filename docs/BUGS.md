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

- **Only the configured `CMakeLists.txt` names the proxy, never one a subdirectory adds.** The scan
  reads `dep.cmakelists_path()` alone (`build_dependency.py:850`). A root that calls
  `add_subdirectory(src)`, where `src/CMakeLists.txt` holds `include(${CMAKE_SOURCE_DIR}/mama.cmake)`,
  reads as a dep that asks for nothing. A leaf with no children then gets no proxy, and cmake fails on
  the include. An indirect `set(M ...)` then `include(${M})` reads the same way. Fix: scan every
  `CMakeLists.txt` under the source dir, or take the ask from the mamafile instead.

- **A targeted run leaves an out-of-scope root without its `mama.cmake` proxy.** A targeted run narrows
  `flat_deps` to the subtree of the target (`main.py:428-431`), so `_save_cmake_files` never runs for a
  dep outside it. Mama never configures that dep either, so no mama run breaks. A user who then runs
  plain cmake in that dir, or opens it in an IDE, finds no proxy and an empty `MAMA_INCLUDES`. Fix: write
  the proxy for every loaded dep whose `CMakeLists.txt` includes it, whatever the scope of the run.

- **`enable_cxx26()` asks for C++23 on GCC and Clang.** `build_target.py` writes `c++2b`, which is the
  C++23 name. MSVC gets `c++latest`, so one mamafile asks for two different standards. Fix: write
  `c++2c`, which GCC 14 and Clang 17 accept, and keep `c++2b` for an older compiler.

- **A `-std` in `add_cxx_flags()` now steers `CMAKE_CXX_STANDARD`.** `cxx_standard()` reads the `-std`
  key of `cmake_cxxflags`, and `add_cxx_flags('-std=c++17')` writes that same key. A plain compiler
  flag therefore changes what mama tells cmake. That is the right answer for `-std=c++17`, and the
  wrong one for a flag that names a standard mama has no min-cmake entry for. Fix: record which
  standard `enable_cxxNN()` chose, and read that instead of parsing the flag back.

- **Compiler discovery composes a suffixed path that a symlinked toolchain does not have.**
  `find_compiler_root` resolves the symlink of a candidate and returns the REAL dir. It keeps the
  suffix that named the link. `get_preferred_compiler_paths` then joins the two
  (`build_config.py:712`), so `/usr/bin/clang++-18 -> /usr/lib/llvm-18/bin/clang++` yields
  `/usr/lib/llvm-18/bin/clang++-18`, and cmake reports "not a full path to an existing compiler
  tool". Only a host whose suffixed compiler links to an unsuffixed name hits it, which is the
  normal Debian and Ubuntu LLVM layout. Fix: return the suffix that the RESOLVED path carries, or
  return the resolved full path instead of the (dir, suffix) pair.

- **A TLS certificate failure marks the network unavailable for the whole run.** `is_network_error`
  answers True for any `URLError` whose reason is an `OSError`, and `ssl.SSLError` is one
  (`mama/utils/net.py:155-161`). A run behind a proxy with an untrusted certificate skips every later
  fetch and clone. The docstring above it promises False for an auth failure. Reproduce by pointing
  `artifactory_ftp` at a host with a self-signed certificate. Fix: test `ssl.SSLError` before the
  `OSError` arm and answer False.

- **The default job count ignores a container CPU limit.** `_default_build_jobs` reads
  `psutil.cpu_count()` (`mama/build_config.py:127`), which reports the host cores inside a
  cgroup-limited container. A 3-CPU runner then defaults to 35 jobs and the OOM killer stops the build,
  unless the run passes `jobs=N`. Fix: read the cgroup v2 quota from `/sys/fs/cgroup/cpu.max` on Linux,
  and take the smaller of the two counts.

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

- **The release-CRT enforcement never reached a project on cmake 3.15 or later.** Policy CMP0091 moved
  the flag, and `mama.cmake` reaches no third-party project. Fix: set it on the configure command line.

- **A package floor below the global one enabled nothing.** The global gate weighed the compiler
  version first. Fix: that gate reads the toolchain alone, and each package weighs its own floor.

- **The module strip deleted an object no exported module named.** A bare name matched a private
  module and a `foo.cpp` build alike. Fix: each module takes the members that share the most path,
  and a bare name answers only when no non-module source of this target carries it.

- **An intermediary archive kept the module objects of its child packages.** Fix: the strip reads the
  modules of every child too, and an archive that compiled none keeps its own path.

- **A second `export_modules()` call lowered the floor of the first.** Fix: the strictest floor wins.

- **A casing variant dropped a module on macOS.** The path compare merged case on Windows alone.
  Fix: `match_path` follows the filesystem, and the upload validation reads it too.

- **The second build published the archive of the first one.** The strip read its own recorded copy,
  which nested one dir per run. Fix: it reads the archive the build wrote.

- **An empty compiler floor broke the whole configure.** An unquoted empty operand left the `if` with
  no right side. Fix: an empty floor refuses, and a package floor falls back to the global one.

- **One skipped module package still defined `MAMA_HAS_MODULES`.** A consumer then imported a module
  the file set never got. Fix: one refused package keeps the headers of every package.

- **The Windows strip found neither `lib.exe` nor its archive members.** Only a developer prompt puts
  the tool on PATH, and an archiver lists a full path. Fix: read the MSVC toolset and match the name.

- **A source-built dependency exported the archive that still held its module objects.** Fix: the
  packaging step points `exported_libs` at a stripped copy under `mama-nomodules/`.

- **The module strip removed every definition the interface unit compiled.** Fix: the API states that
  an exported module defines nothing but its interface, and every strip warns.

- **One lowered compiler floor enabled every module package.** Fix: `export_modules` takes
  `min_gnu`/`min_clang`/`min_msvc`, and the cmake helper weighs each package on its own floor.

- **A recursive package shipped a child module the child package also shipped.** A consumer then
  declared one module twice. Fix: write `M` records for the deployed target alone.

- **A `PUBLIC` module file set broke a consumer that installs and exports its own target.** Fix:
  `mama_target_modules(target [scope])` takes an optional scope.

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
