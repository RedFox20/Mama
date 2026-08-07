# Known bugs

Two lists. Open defects first, then the fixed ones. Newest first in both.

Add an entry to **Open** when you find a bug you do not fix in the same change. An open entry carries
the reproduction, the `file:line` of the cause and the shape of the fix. A bug that needs more than
five lines gets its own handover doc, and the entry links to it.

**When you fix an entry, compact it and move it to Closed, in the same commit.** A closed entry keeps
two things: what the bug did, and how the fix works. Drop the reproduction, the line numbers and the
design options. The commit and its tests hold that detail, and a copy here goes stale.

## Open

- [ ] **A shim of another platform keeps serving a deleted package.** `local_copies` in
  `artifactory_unpublish.py` reads `dep.build_dir`, which is one platform. An unpublish on a linux run
  leaves `packages/<ws>/mylib/android/` holding a shim marker for an archive the server no longer has,
  which is the failure the module exists to prevent. Walk every build dir under `dep_dir` instead, and
  drop each whose marker names a deleted archive.

## Closed

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
