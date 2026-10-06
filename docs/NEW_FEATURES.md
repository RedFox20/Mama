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

- **A GNU configure project under wasm.** `gnu_project.py` runs `./configure` and `make` with no
  Emscripten wrapper, so a wasm build of such a project compiles for the host. Shape: `Wasm` names
  `emconfigure` and `emmake` as the wrappers, and `GnuProject` runs its steps through them.

- **On MSVC, build a dependency in the configuration of the root.** `BuildTarget.cmake_build_type` gives
  every dependency `Debug` when the run is debug, and a root mamafile commonly picks `RelWithDebInfo`.
  On MSVC the configuration name also picks the artifact name, so a dependency that sets
  `CMAKE_DEBUG_POSTFIX` produces `<name>_d.dll` and the consumer stages nothing. Shape: let the root
  publish its `cmake_build_type` and have an MSVC dependency inherit it, or export a configuration-aware
  product name. The CRT half of the same split is already fixed on the configure command line.

## Implemented

- **`unpublish=since=<age>` and `dependents` remove the packages an ABI break made unsafe.** `since` deletes every archive
  uploaded in the last `90m`, `6h` or `2d`, by the upload time the server reports. `dependents` swaps the
  scope to every package whose subtree holds the named target, so `mama ReCpp unpublish=since=6h
  dependents` reaches every package built on ReCpp, on every platform. The run loads the whole graph.

- **`config.wasm.enable_threads()` builds a threaded wasm tree.** The root mamafile calls it in
  `settings()`. Every target then compiles and links with `-pthread`, and the `mt` variant token gives
  the tree its own build dir and archive name, eg `wasm-mt`. A later call only prints a warning. The web
  server must send the COOP and COEP headers with the page that loads the program.

- **`mama build wasm` builds WebAssembly with the Emscripten SDK.** mama finds the SDK in `EMSDK`, `~/emsdk`
  or at the `emcc` on `PATH`, and builds with its `Emscripten.cmake` and Ninja. Every target that enables
  exceptions compiles with `-fwasm-exceptions`, every link gets it, and no object gets `-fPIC`.
  `mama test wasm` runs the test program under node, and `mama_wasm_test()` in `mama.cmake` links it for that.

- **`mama build raspi` finds the Pi SDK by itself.** It takes its env vars (`PI_SDK_HOME` first), then the
  newest finished `/opt/pi-sdk/<version>`, then `~/pi-sdk`, before its legacy paths and the distro cross
  package. A `GnuCross` board lists such roots in `sdk_roots`, and an install counts only when its
  `.installed` file exists.

- **MSVC builds with Ninja.** `generator=ninja`, `MAMA_GENERATOR=ninja` or `set_default_generator('ninja')`
  in the root mamafile moves every target that configures to Ninja. mama loads the vcvarsall env, names `cl.exe` and
  drops `/MP` for an MSVC target outside Visual Studio. `generator=native` goes back.

- **`coverage` instruments the target the user named, not the whole tree.** `mama coverage app test`
  builds `app` in `<platform>-cov`. Every other dep keeps the dir a plain run uses, so a warm tree
  rebuilds one target instead of all of them. `mama coverage all` still instruments every dep.
  A dep outside the coverage target uploads the archive a plain run uploads, and an instrumented dep
  refuses to upload at all. No artifactory package then carries the `.gcda` paths of one machine.
  `BuildConfig.instruments(dep)` is the one predicate, and a dep that only LINKS an instrumented dep
  still gets `--coverage` for libgcov.

- **`mama lock platforms=...` freezes Git dependencies across platform graphs.** The generated
  `mama.lock` records repositories, declared selectors and exact commits. Normal builds honor it,
  while targeted lock refreshes can select the current declared ref or an older reachable commit.

- **A package ships its C++20 module interface units, and the consumer compiles them.** A binary
  module interface is not portable. Mama exports every module under an exported include dir with no
  declaration, and `export_modules(path, [names])` only narrows it. Modules deploy inside the include
  tree, one `M` record each. `mama-dependencies.cmake` sets `{name}_MODULES` and the aggregate
  `MAMA_MODULES`, and `mama_target_modules(<target> [scope])` adds the file set, `cxx_std_20` and the
  module scan. A toolchain below cmake 3.28, Ninja 1.11, GCC 14, Clang 18 or MSVC 19.34 keeps the
  headers and says so, as does `MAMA_ENABLE_MODULES=OFF`. The packaged static library loses its module
  objects, because a consumer compiling the same module defines the same initializer.
  `strip_objects=False` keeps them.

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
