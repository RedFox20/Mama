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

- [ ] **A target that exports nothing uploads an empty archive.** This is a defect, and the repair adds
  a capability, so it lives here. A docs-only or bundle-only target builds nothing and exports nothing,
  yet `mama upload` still publishes an archive holding one `papa.txt`. That package is worthless. A
  consumer that fetches it gets no headers and no libs, and the run continues as if the dependency
  resolved.

  Two halves:
  1. **Refuse the upload.** `validate_archive` at `papa_upload.py:112` already seeds `expected` with
     `papa.txt` and rejects an include dir that holds no files. It does not reject an archive whose only
     entry IS `papa.txt`. Add that check, so no empty archive can reach the server by any route.
  2. **Set `nothing_to_upload` automatically.** `_run_packaging` at `build_target.py:1768` knows what
     the packaging produced. When a target exports no includes, no libs, no syslibs and no assets, mark
     it. The refusal above then stays a backstop, not the thing users meet.

  Today a mamafile has to say so by hand, which nobody discovers until a broken package ships:

  ```py
  def settings(self):
      self.nothing_to_upload()
  def package(self):
      self.no_export_includes()
      self.no_export_libs()
  ```

  Keep `nothing_to_upload()` working as an explicit override. A target may want it before the packaging
  runs at all.

## Implemented

None yet.
