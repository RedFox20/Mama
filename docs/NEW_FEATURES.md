# New features

Two lists. Planned work first, then what shipped. Newest first in both.

Add an entry to **Planned** when a feature is agreed but not written yet. An entry carries what a user
gains, the `file:line` the work starts from, and the shape of the change. A feature that needs more
than eight lines gets its own design doc, and the entry links to it.

**When you ship an entry, compact it and move it to Implemented, in the same commit.** An implemented
entry keeps two things: what a user can now do, and how it works. Drop the design options and the line
numbers. The commit, its tests and `docs/SPEC.md` hold that detail, and a copy here goes stale.

A defect belongs in `docs/BUGS.md`, unless the repair is a new capability. Then it belongs here.

## Planned

None.

## Implemented

- **A target that exports nothing no longer publishes an empty archive.** The packaging marks such a
  target `no_upload` on its own, so a docs or bundle target needs no declaration. `validate_archive`
  refuses an archive holding only `papa.txt` as a backstop. `nothing_to_upload()` still works by hand,
  and the automatic mark never clears it.

- **`unpublish=<selector>` deletes published archives.** `current`, an explicit version, `prune-old[=N]`
  and `prune-all` each name a set. One selector reaches every platform and compiler, because the version
  is the trailing field of the archive name. The run lists each archive with its date and size and asks
  first, then removes the local cached zip and any shim that served one.
