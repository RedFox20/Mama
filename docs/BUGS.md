# Known bugs

Open defects, newest first. Tick the box in the commit that fixes the bug, and delete the entry when
the fix ships in a release. A bug that needs more than five lines here gets its own handover doc, and
this list links to it.

- [ ] **`mama build debug` after `mama build` builds release, and says nothing.**
  Debug and release share one build dir on purpose, but a build-type flip does not force a configure.
  `must_configure` at `buildsys/cmake/configure.py:390` reads only `update` and `run_cmake_configure`,
  and `debug` sets neither. `run_config` then returns early on the existing cache, which still holds
  `CMAKE_BUILD_TYPE=RelWithDebInfo`. `--config Debug` reaches `cmake --build`, so a multi-config
  generator such as MSVC is fine. A single-config generator, Ninja or Make, silently builds release.
  The build type is already part of the configure fingerprint, so adding it to `must_configure` starts
  the fix.

  What it should do instead:
  1. Detect the build-type flip, then reconfigure and build the named target. A dep that already built
     stays as it is, so a debug run of the root costs one target, not the tree.
  2. Warn that the run mixes release and debug packages, and name every package that holds the other
     build type. Each dep records its own type in its `CMakeCache.txt`.

  The mix stays allowed. A developer often needs one target in debug to read a stack trace. A rebuild
  of every dep to get it costs more than the stack trace is worth.

- [ ] **A revived deferred dep with a parent-supplied mamafile crashes the run.**
  `revive_deferred_load` clears `already_loaded` and `target`, but it keeps `children` and leaves
  `did_skim` False. The second `_load` therefore runs `dependencies()` again, and `add_child` raises
  `has already been added`. It needs a deferred load that named children, which means an `add_git`
  with a `mamafile=` override. A dep with no clone has no mamafile of its own to read.
  Reproduced: `mama build <target>` where a sibling dep uses the `mamadeps/` pattern raises
  `BuildTarget foo add dependency 'grandchild' failed because it has already been added`.
  The fix is for `revive_deferred_load` to drop the children the deferred load named.

- [ ] **`mama update` cannot move a stale shim forward when no new package exists.**
  Under `update`, `_try_artifactory_shim` skips the cached path, so nothing drops the marker. The
  probe then misses, and `_git_checkout_if_needed` returns False for any shim, so no clone happens.
  The dep silently keeps the package of the old commit. Under `noart` the same dep does detect the
  move and clones, so the two flags disagree about the same state.

- [ ] **A root project with no `mamafile.py` writes its packages into the user home dir.**
  Only the mamafile parse assigns `config.workspaces_root`, at `build_dependency.py:740`. A
  CMakeLists-only root parses no mamafile, so the field keeps its `HOME` default and every dep dir
  lands in `~/packages/`. Verified: a bare `CMakeLists.txt` project resolves its root dep dir to
  `/home/<user>/packages/<name>`. The fix is to default `workspaces_root` to the root source dir.

- [ ] **`default_package()` runs the default packaging only when told not to.**
  `build_target.py:1322` reads `if self.no_includes: self.default_package_includes()`, and
  `no_export_includes()` sets `no_includes = True` to mean the opposite. The same inversion applies to
  `no_libs`. `_run_packaging` is unaffected, because it guards with `not self.no_includes` itself, so
  only a mamafile that calls `self.default_package()` by hand hits this.

- [ ] **An artifactory package deploy ignores `includes_filter`.**
  Mama skips `package()` when a target loads from artifactory, so the papa deploy falls back to the
  default header filter `['.h', '.hpp', '.hxx', '.hh']`. A recipe that asked for another suffix loses
  those headers from the deployed tree and from the uploaded archive. Every consumer of that package
  then fails to compile. Reported against 0.13.12.
  Detail: [BUG_HANDOVER_INCLUDES_FILTER.md](BUG_HANDOVER_INCLUDES_FILTER.md)

- [x] **`test_a_changed_source_never_arms_the_gate` fails about one run in three.**
  `tests/test_source_fingerprint/test_gate_arms.py:58`. The test edits a file and expects the walk
  gate to report a change. `source_walk_moved` compares filesystem timestamps, and an edit inside the
  same timestamp tick reads as no change. Either the gate needs a finer signal than mtime, or the test
  needs to force the clock forward.
