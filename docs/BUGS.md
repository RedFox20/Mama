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
