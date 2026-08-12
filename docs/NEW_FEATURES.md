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

- **On MSVC, build a dependency in the configuration of the root.** `BuildTarget.cmake_build_type` gives
  every dependency `Debug` when the run is debug, and a root mamafile commonly picks `RelWithDebInfo`.
  On MSVC the configuration name also picks the artifact name, so a dependency that sets
  `CMAKE_DEBUG_POSTFIX` produces `<name>_d.dll` and the consumer stages nothing. Shape: let the root
  publish its `cmake_build_type` and have an MSVC dependency inherit it, or export a configuration-aware
  product name. The CRT half of the same split is already fixed on the configure command line.

## Implemented

- **`export_modules(path, [names])` ships C++20 module interface units to a consumer.** A binary
  module interface is not portable, so a package cannot ship one. The consumer compiles the sources
  instead. The modules deploy inside the exported include tree, one `M` record each, and the module
  reaches its own header from there. `mama-dependencies.cmake` sets `{name}_MODULES` and
  `MAMA_MODULES`, and `mama.cmake` carries `mama_target_modules(<target> [scope])`, which adds the file set
  and the `cxx_std_20` feature the module scanner needs. A toolchain below cmake 3.28, Ninja, GCC 14,
  Clang 21 or MSVC 19.34 keeps the headers and says so, so no build fails on this. The packaged
  static library loses its module objects, because a consumer that compiles the same module defines
  the same `initializer for module X` symbol. `strip_objects=False` keeps them, for a target whose
  own sources import its own module.

- **`config.set_target_march(arch, march)` pins the instruction set of a release build.** The native
  default is `-march=native`, which bakes the build machine's CPU into the binary. The root mamafile
  pins one value per target arch, and the pin replaces the platform default for the root and every
  dependency. It raises on an unknown arch and on a value that is not the `-march` value alone. The pin
  renames the arch field of the artifactory archive, `x64` plus `x86-64-v3` into `x64v3`, so a tuned
  package cannot download over a baseline one. The build dir keeps its name, because a project hardcodes
  that path. The `O` record of papa.txt keeps the real value next to the arch.

- **A target that exports nothing no longer publishes an empty archive.** The packaging marks such a
  target `no_upload` on its own, so a docs or bundle target needs no declaration. `validate_archive`
  refuses an archive holding only `papa.txt` as a backstop. `nothing_to_upload()` still works by hand,
  and the automatic mark never clears it.

- **`unpublish=<selector>` deletes published archives.** `current`, an explicit version, `prune-old[=N]`
  and `prune-all` each name a set. One selector reaches every platform and compiler, because the version
  is the trailing field of the archive name. The run lists each archive with its date and size and asks
  first, then removes the local cached zip and any shim that served one.
