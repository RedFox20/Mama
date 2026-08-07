# Known bugs

Two lists. Open defects first, then the fixed ones. Newest first in both.

Add an entry to **Open** when you find a bug you do not fix in the same change. An open entry carries
the reproduction, the `file:line` of the cause and the shape of the fix. A bug that needs more than
five lines gets its own handover doc, and the entry links to it.

**When you fix an entry, compact it and move it to Closed, in the same commit.** A closed entry keeps
two things: what the bug did, and how the fix works. Drop the reproduction, the line numbers and the
design options. The commit and its tests hold that detail, and a copy here goes stale.

## Open

- [ ] **`test_a_shape_only_git_can_settle_defers[gitdir file]` fails about one Windows run in ten.**
  `tests/test_git_repo_heal/test_repo_health.py:75` calls `shutil.move` on a `.git` dir that git wrote
  a moment earlier. On Windows a move fails while any handle on the tree is open, and a virus scanner
  holds one briefly. Seen once under `-n8`, then 13 clean runs, and the traceback was not captured.
  Confirm the error is `PermissionError` before you fix it. The likely fix is a short retry around the
  move, in a test helper. Linux never hits this, because a rename there ignores open handles.

## Closed

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
