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

None.

## Closed

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
