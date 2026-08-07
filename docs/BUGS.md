# Known bugs

Two lists. Open defects first, then the fixed ones. Newest first in both.

Add an entry to **Open** when you find a bug you do not fix in the same change. An open entry carries
the reproduction, the `file:line` of the cause and the shape of the fix. A bug that needs more than
five lines gets its own handover doc, and the entry links to it.

**When you fix an entry, compact it and move it to Closed, in the same commit.** A closed entry keeps
two things: what the bug did, and how the fix works. Drop the reproduction, the line numbers and the
design options. The commit and its tests hold that detail, and a copy here goes stale.

## Open

- **A multi-config generator offers three configurations that cannot link.**
  `buildsys/cmake/configure.py:445` passes `CMAKE_BUILD_TYPE` alone, so a Visual Studio or Xcode
  generator keeps the CMake default set of `Debug`, `Release`, `RelWithDebInfo` and `MinSizeRel`.
  Mama configures and links one of them. The other three reach the IDE dropdown with no dependency
  artifacts behind them, so a build of one cannot link. A consumer also has to read `CMAKE_BUILD_TYPE`
  back out of `CMakeCache.txt` to learn what mama chose, because `cmake --install` needs `--config`
  and `ctest` needs `--build-config`. Fix: pass `CMAKE_CONFIGURATION_TYPES` when
  `_MULTI_CONFIG_GENERATORS` matches, defaulting to `RelWithDebInfo;Debug`.

- **`host_build_dir` names a bare platform dir the bootstrap child may not build into.**
  `build_target.py:host_build_dir` joins the dep dir with `config.host_platform_name()`, which answers
  `linux`, `windows` or `macos` and nothing else. The `mama <host> build` child reads the same root
  mamafile. A root `prefer_clang()` sends it to `linux-clang`, a consumer dep arg to `linux-lgpl`, and
  an arm64 Linux host to `linuxarm`. `build_host_binary` then probes a path the child never wrote
  and reports the host tool as missing, although the child exited 0. A warm tree hides it, so only a
  clean checkout fails. Fix: name the host dir through `build_names.build_dir_name` for a host config,
  instead of the platform name alone.

## Closed

- **An `-march` pin renamed every build dir and broke every consumer of that path.** The pin joined the
  build dir name, so a pinned x64 linux tree moved to `linux-x64v3`. `host_build_dir` names the bare
  platform dir, and the bootstrap child inherits the pin from the root mamafile, so the child built one
  path and the host-binary probe read another. A warm tree hid it, so only a clean checkout failed. The
  pin now renames the artifactory archive alone, which is where a tuned package has to stay apart.

- **A shim of another platform kept serving a deleted package.** The local purge read one build dir, so
  an unpublish on a linux run left the android shim naming an archive the server no longer had. It now
  walks every build dir of the dep, and drops each whose marker names a deleted archive.

- **A `clean` run never unpublished, and said nothing.** `clean_only` returns before the usual call
  site. That path now runs the unpublish itself, and the cached zips survive a clean because they live
  in `dep_dir` rather than the build dir.

- **`test_failure_fires_abort_hook_once_to_kill_in_flight` failed once and never again.** Judged a
  fluke by the owner after 26 suite runs and 7300 direct iterations reproduced nothing. The assert is
  split, so a repeat names which half broke.

- **A Windows test moved a `.git` dir that git had just written.** A rename there refuses while any
  handle on the tree is open, so the `gitdir file` shape failed once under parallel workers. `git init
  --separate-git-dir` now writes that shape itself, so no move happens at all.

- **An offline `mama update` ended the run over a package it already had cached.** `update` skips the
  cached zip to fetch a fresher copy, and nothing used that zip when the download answered nothing.
  A failed download now falls back to the cached archive of the same name.

- **`noart` left an `add_artifactory_pkg` dep with no package.** The flag refused the fetch of every dep,
  and a package dep has no source to clone instead, so it exported nothing. A package dep now ignores
  `noart`, a rebuild and a clean, and it refuses a deploy or an upload.

- **A deploy re-rooted an include tree that an archive already unpacked.** `as_includes_root` applied
  its alias to a fetched package too, so every republish nested the tree one level deeper.
  `_include_deploy` now copies an unpacked archive as it stands.

- **`mama build debug` after `mama build` built release, and said nothing.** Debug and release share one
  build dir, and a build-type flip forced no configure, so a single-config generator reused the release
  cache. The configure step now compares the type in `CMakeCache.txt` with the run, and reconfigures on a
  flip. The run also names every package that holds the other type. The mix stays allowed.

- **A revived deferred dep with a parent-supplied mamafile crashed the run.** The revive kept the children
  the deferred load named, so the second `dependencies()` call made `add_child` refuse a duplicate.
  `revive_deferred_load` now drops the children, and the dep registry re-attaches the same instances.

- **`mama update` could not move a stale shim forward when no new package answered.** The probe missed,
  nothing dropped the marker, and the dep kept the package of the old commit. An `update` now drops a
  marker whose commit upstream has left behind, and the git path clones the source.

- **A root project with no `mamafile.py` wrote its packages into the user home dir.** Only a mamafile parse
  assigned `config.workspaces_root`, so a CMakeLists-only root kept the `HOME` default. The root load now
  defaults the field to the root source dir.

- **`default_package()` ran the default packaging only when told not to.** The `no_includes` and `no_libs`
  guards were inverted. Both now read `not`.

- **An artifactory package deploy ignored `includes_filter`.** Mama skipped `package()` for a fetched
  target, and `papa.txt` records the export list but never the export rules. `package()` now always runs,
  and a fetched target keeps the exports it already had when the hook cannot read a build product.

- **`test_a_changed_source_never_arms_the_gate` failed about one run in three.** The walk gate compares
  filesystem mtimes, and an edit inside one timestamp tick reads as no change. The test now waits 10 ms
  before it edits.
