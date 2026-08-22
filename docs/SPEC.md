# Mama behavior specification

What mama does, under every command, in every state. The code is the truth. This file is the intent,
and a change that contradicts it is a bug in one of the two. Update this file in the commit that
changes the behavior.

Read the section that covers what you are about to change, before you change it.

A **Why:** line states the reason a behavior exists. It is design rationale, not a behavior claim, so
an audit against the code cannot confirm it. Every other line is a claim the code must answer for.

| # | Section | Covers |
|---|---|---|
| 0 | [Why mama exists](#0-why-mama-exists) | the goal, and what each feature replaces |
| 1 | [Vocabulary](#1-vocabulary) | dep, target, root, workspace, the three dirs, shim, papa |
| 2 | [Disk layout](#2-disk-layout) | what mama writes, and where |
| 3 | [The run](#3-the-run) | phase order, the two execution paths |
| 4 | [Commands](#4-commands) | every action, and what it refuses to do |
| 5 | [Flags](#5-flags) | every CLI argument |
| 6 | [Dependency sources](#6-dependency-sources) | git, local, artifactory package |
| 7 | [The load](#7-the-load) | states, skim, defer, revive, the claim, locks |
| 8 | [Artifactory](#8-artifactory) | the shim, archive naming, build vs update vs noart |
| 9 | [Versions](#9-versions) | self.version, pins, local content version |
| 10 | [Rebuild decision](#10-rebuild-decision) | every reason a target builds |
| 11 | [Configure decision](#11-configure-decision) | skip, wipe, fingerprints, the seed cache |
| 12 | [Build and packaging](#12-build-and-packaging) | phases, custom build(), exports |
| 13 | [Deploy and upload](#13-deploy-and-upload) | papa records, the upload guard, unpublish |
| 14 | [Test, start, open](#14-test-start-and-open) | the source requirement |
| 15 | [Platforms](#15-platforms) | pointer to the platform architecture |
| 16 | [Output](#16-output) | the live display, the build log, git filtering |
| 17 | [Concurrency](#17-concurrency) | threads, locks, fetch slots, abort |
| 18 | [Environment](#18-environment) | the variables mama reads |

---

## 0 Why mama exists

Mama is a **project-based, from-source, actively-modifiable** dependency and build tool. It **follows
the latest** by default. `mama update` fetches a branch dep and hard-resets it to `origin/<branch>`, so
the tree matches upstream even after a force-push. A pin wins over that default: a tag, or a commit,
which mama keeps in the same field. A branch declared beside a tag still wins the checkout. A
`self.version` pins the artifactory archive name, not the git revision. There is no lockfile and no
version solver. Mama builds for **several platforms side by side**. The whole point
is that `mama build` and then `mama android build` both work, from one checkout, with no clean between
them.

Each feature answers a specific pain.

1. **Git submodules are annoying to maintain.** A mamafile names a dep in one line, and mama owns the
   clone.
2. **A CMake subproject joins the main build.** A clean or a rebuild of a very large project then costs
   a full rebuild of everything. Mama gives each dep its own build dir, so a clean scopes to one target.
3. **Hand-managed sub-clones go stale.** `mama update` moves every dep forward in one command.
4. **A package needs edits before you publish it.** Edit it in place, build and test it inside the
   bigger project, then publish. `mama build` must never overwrite a local modification inside a
   package. This one is not negotiable, see section 7.
5. **A cmake configure is slow.** Mama configures deps in parallel, and it caches the compiler
   detection that cmake would otherwise repeat per build dir.
6. **A slow build often uses little CPU.** Mama builds deps in parallel and gates them on a core
   budget, so a link-bound target does not hold the machine.
7. **A package that rarely changes wastes every recompile.** Artifactory serves a prebuilt archive for
   a source version that already built on this platform, arch, compiler, build type and variant.
8. **A clone can be slow.** The shim downloads that archive with no working tree. It resolves the
   commit by ls-remote, and only a dep that may pin `self.version` costs one throwaway blob-less clone.
9. **Link order is a recurring bug.** Mama derives the flat dep list and the link order from the graph.
10. **Testing needs scaffolding.** `mama build test=<args>` builds, then hands `<args>` to the `test()`
    hook of the mamafile, which decides what to run. The `gtest()` helper reads them as a test filter.

## 1 Vocabulary

One name for one thing. This file uses these and no synonyms.

- **dep** - a `BuildDependency`. One node of the graph. It owns the dirs and the load state.
- **target** - a `BuildTarget`. The object a mamafile subclasses. One dep owns one target.
- **root** - the dep of the project mama runs in. It has no parent, and it always builds.
- **current target** - the dep the command names. `config.target_matches(name)` decides, and `all`
  matches every dep.
- **workspace** - the directory that holds every dep dir. `packages` by default.
- **dep_dir** - `<workspaces_root>/<workspace>/<name>`. It holds one build dir per platform.
- **src_dir** - where the source lives. For a git dep, `<dep_dir>/<name>`. For a local dep, the path
  the parent declared. An artifactory package has none.
- **build_dir** - `<dep_dir>/<build_dir_name>`. See section 2 for the name.
- **shim** - a git dep satisfied by an artifactory package with no working tree. A `mama_shim` marker
  in its build dir plus no `.git` dir proves it. A marker beside a real clone is stale, and mama drops it.
- **papa** - mama's package format. `papa.txt` lists what a package exports.

## 2 Disk layout

```
<workspaces_root>/<workspace>/          the workspace, `packages` by default
  mamabuild.log                         the log of the last run
  .mama/locks/<name>.lock               the cross-process load lock of each dep
  .mama/compiler_seed/                  the cmake compiler-detection cache
  <name>/                               a dep dir
    <name>/                             src_dir, a git dep only
      mama.cmake                        the proxy, for the deps that get one
    <build_dir_name>/                   one per platform, arch, compiler and variant
      CMakeCache.txt, papa.txt, mama_shim, git_status, mamafile_tag, mama_exported_libs
      mama-dependencies.cmake           what the dependencies export
    <archive>.zip                        the cached artifactory package
```

`workspaces_root` is the root project dir, unless the root mamafile declares `global_workspace`, which
keeps it at the user home dir. A root with no `mamafile.py` at all keeps the project dir too.

**Mama generates two cmake files, and only one of them reaches a source dir.**
`<build_dir>/mama-dependencies.cmake` names every include dir and lib that this dep and the deps below
it export, and mama writes one for every dep that has a build dir. `mama.cmake` is the proxy a
consumer's `CMakeLists.txt` includes. It detects the platform and the arch the way cmake sees them, then
includes that build dir's `mama-dependencies.cmake`. **It goes to every path the `include()` commands
name.** A conditional include names one path per branch, and mama writes them all, because cmake alone
knows which branch runs. Mama resolves each path against the dir cmake configures. That dir is
`<src_dir>` for the default `cmake_lists_path`. It is the dir of the named file when a mamafile points
`cmake_lists_path` at a nested or an absolute one. Mama expands `CMAKE_CURRENT_LIST_DIR`,
`CMAKE_CURRENT_SOURCE_DIR`, `CMAKE_SOURCE_DIR` and `PROJECT_SOURCE_DIR` to that dir. An argument that
still holds a `$` after that names a form mama does not expand, and it takes the default `mama.cmake`
beside the `CMakeLists.txt`. **A path that leaves the source dir and the dir cmake configures gets a
warning and no file.** The test resolves every symlink first, so a link inside the source dir leads
nowhere new. An absolute `cmake_lists_path` widens the area mama may write to, because the dir it names
is a dir cmake configures. A dep whose every include is refused still takes the shape rule below.
`mama.cmake` is a generated name, and mama overwrites one wherever it does write.

**A dep gets the proxy when its `CMakeLists.txt` asks for it, or when its shape says it needs one.** An
`include()` whose first argument has the basename `mama.cmake`, in either case, asks for it, whatever
else the dep holds. It still needs a source dir and that `CMakeLists.txt` on disk. A longer name such as
`grandmama.cmake` is a module of the project, and mama never writes over it. The scan reads the whole
file, because a cmake command may span lines, and it matches the command name in either case, because
cmake does. One pass reads the quoted arguments, the bracket arguments and the comments together. A `#`
inside a string opens no comment, and none of the three can name the proxy. A quoted path may hold a
space. Any other dep needs a source dir, children, a mamafile and a `CMakeLists.txt`, because a leaf
has no dependency includes or libs to name.

**The scan caches nothing**, because a `configure()` hook can rewrite a `CMakeLists.txt` in place with
no change that a `stat` can see.

**A guard follows each write.** Every path mama writes must hold a proxy after that write. The run stops
when one does not, and the error names the dep, the path and the `CMakeLists.txt`.

**The cmake configure step writes the proxy again**, because the `configure()` hook of a mamafile can
move `cmake_lists_path` after the load already wrote one.

**Why:** the proxy is a generated file inside a checkout. A dep that does not use it pays with an
untracked file for the life of the working tree. The write runs two lines before the guard, so only a
failed write reaches it. Without the guard cmake reports a missing header of an unrelated project
minutes later, and every `MAMA_` variable reads as an empty string.

**`build_dir_name` = `<platform dir><-clang><variant>`**, coarsest axis first.

- The platform dir comes from the platform class, which maps each arch to its own name. The primary
  arch uses the bare name (`linux`, `windows`), and every other arch gets its own (`linux32`,
  `linuxarm`, `windows32`, `macosarm`).
- `-clang` appears only on a Linux clang build. gcc keeps the bare name, so existing trees do not
  churn. Elsewhere the toolset or the SDK already fixes the compiler.
- The variant is `build_variant_suffix`: `-cov` for coverage, then one token per sanitizer, then the
  dep args. Each token gets its own `-`. A plain build with no args adds nothing.
- Dep args are sorted, lowercased, de-duplicated and stripped to ASCII alphanumerics. `+` becomes
  `p`, so `C++20` is `cpp20`. `NEWMATH=1` is `newmath1`, distinct from `newmath2`.

**An `-march` pin NEVER renames a build dir.** It renames the arch field of the artifactory archive
name and nothing else. The root mamafile owns the pin, so it is constant for a checkout and two pins
can never meet in one tree. A build dir is also a path a project hardcodes, in `cmake --install`, in
CI and in its own cmake. Renaming it breaks every one of those at once.

The one in-tree consumer is `host_build_dir`, which named the bare platform dir plus nothing else. The
`mama <host> build` bootstrap child reads the same root mamafile, so it inherits every setting that
mamafile makes. A pin in the dir name made the child build one path and `build_host_binary` probe
another. The host tool then went missing while the child reported success.

**`host_build_dir` names the dir through the same rules the child follows.** `host_build_dir_name`
builds a host view of the config, then runs the two functions above on it. The host platform, the arch
of this machine, the compiler this run resolved and the dep args all reach the name, so an
`args=['LGPL']` dep names `linux-lgpl` and an arm64 Linux host names `linuxarm`. The child resolves its
own graph, so this name is what the child MOST LIKELY writes, and the search below covers the rest.

**The host view names the arch of this machine, never the platform default.** macOS defaults to arm64,
and an Intel Mac cannot run an arm64 tool. `build_host_binary` passes the same arch to the child, so the
child cannot fall back to a default that names another machine.

**The child never gets this run's coverage or sanitizer flag, so neither names the host dir.** A host
tool is a tool. `build_host_binary` passes a compiler to the child only when the command line of this
run named one, and only on a Linux host, where the build dir carries a compiler token. A mamafile
preference belongs to the child's own config, and forcing it would build the tool with a compiler the
project refused.

**A build is the host build when the platform matches and the host can RUN the arch.** `Platform.also_runs`
declares what each host runs besides its own arch. An x86 build of an x64 host is a host build. An x64
build is one on Apple silicon, but only on a Mac that has Rosetta 2, and one on an arm64 Windows, but
only on Windows 11, which added that emulator. An arch the host cannot run is a cross build, whatever its
platform says.

**Before the bootstrap the predicted dir answers alone.** It carries the compiler and the dep args of
this run, and a dep arg changes what a tool does. A warm `linux-lgpl` must never serve a run that asked
for `linux`, so a neighbour never answers a probe.

**After the bootstrap the predicted dir answers first, and then only a tool that child PRODUCED.** The
child resolves its own dep args, so the predicted name is a first guess and not a promise. Mama reads
every host build dir before the child runs and again after it, and takes the newest file that changed.
An exit code of 0 does not prove that a tool a warm tree already held belongs to this request. The search opens only names
that start with the host platform dir, so it can never answer with a binary of another arch. It skips a
coverage or a sanitizer dir, and it skips the source dir of a dep whose name opens with the same word.

**Why:** a warm tree hides that failure. A checkout built before the pin already holds the tool under
the old name. Only a clean checkout fails, which means CI and not the developer who made the change.
- **Every marker opens with an arch name.** A pin that names a CPU, such as `haswell`, gets the arch in
  front of it and reads `x64haswell`. That is what keeps `clean all` from deleting a foreign pinned tree.

**Every `(platform, arch, variant)` pair gets its own build dir, and Linux also splits gcc from clang.**
A shared dir means two builds clobber each other's cache and libraries.

**Debug and release deliberately share one build dir.** The name carries no build-type token, so
`mama build debug` reuses the dir `mama build` used. A build-type flip forces a reconfigure, because a
single-config generator bakes `CMAKE_BUILD_TYPE` into the cache.

**The flip never cascades.** The check lives inside the cmake configure, which only a target with real
build work reaches. A dep that is up to date configures nothing, so it keeps its own build type.
`mama build <X> debug` therefore moves X alone, and the run then names every package that holds the
other type and builds anyway. The artifactory archive name does carry `release` or
`debug`, so the two packages never collide on the server.

**Why:** mama lets a project mix release and debug packages, so the build type belongs to the package
name and not to the tree. A developer often needs one target in debug to read a stack trace, and a
rebuild of every dep to get it costs more than that. So the mix warns, and never fails.

A sanitizer is the opposite case. `-asan` and `-tsan` each get their own
tree, because a sanitizer build that shares a tree with a plain one reports false positives.

The variant suffix is spelled in exactly one place, `build_names.build_variant_suffix`. The build dir
name and the artifactory archive name both read it, so they cannot disagree.

## 3 The run

`mamabuild(args)` runs these steps in this order.

1. `help` and `version` answer from the raw argument list and exit, before anything is parsed.
2. Parse the args into a `BuildConfig`. An unrecognized bare word becomes the target name. An
   option-shaped unknown arg (`-foo`, `jobz=4`) fails at once, because it can never be a target.
3. `init` with no target, and the install utilities, return here.
4. Refuse when neither `mamafile.py` nor `CMakeLists.txt` exists.
5. `update` and `deps_only` with no target rewrite `config.target` to `all`.
6. `rebuild` sets `build` and `clean`.
7. `clean` with no target cleans the root build dir.
8. One `git status` for the whole run, so every local dep reads one process instead of spawning its own.
9. **Load the root.** Its `settings()` locks the compiler that names every dep dir below it, and its
   mamafile names the workspace. Its output reaches the terminal directly, never a display, so a
   mis-picked toolchain is visible.
10. **Open the one build log** of the run, under the workspace the root just named.
11. A `sched_debug` run prints the build-weight table and returns. Else pick the execution path below,
    then run it.
12. `list` prints the package listing. Then `coverage-report`, or a `test` run built with coverage,
    prints a coverage report and returns. `open` runs last.

### The two execution paths

**Unified** (`execute_unified`) requires `build` or `update`, plus either no specific target (none, or
`all`) or `deps_only`. It refuses `list`, `dirty`, `init` and `serial`.

One scheduler interleaves clone, configure and build. Each completed LOAD grows the graph with its
children's jobs. A CONFIGURE waits on its own LOAD plus every child's BUILD, so a leaf builds while a
deeper dep still clones.

**Classic** handles everything else, because those commands need the resolved tree up front for lookup
and filtering. A non-targeted or `dirty` run loads the whole graph with `load_dependency_chain`. A
targeted run takes the two-stage walk of section 7 instead. Either way `execute_task_chain_parallel`
then runs a second scheduler over configure and build. `serial` selects `execute_task_chain`, which
runs one dep at a time and draws no display.

Deploy, run and test are serial and children-first on both paths.

## 4 Commands

An action names what the run does. Several may combine.

| Action | What it does |
|---|---|
| `init` | write a starting `mamafile.py`, `CMakeLists.txt` and `src/` main file. It patches an existing `CMakeLists.txt` |
| `list` | load the graph, build nothing, print the exports of every dep, or of the named target alone |
| `build` | configure and build. It clones a missing dep, and it does not pull |
| `update` | check the remote of every dep, pull the ones that moved, then build. A tag pin fetches only when the tag is missing |
| `clean` | delete the build dir of the target. With no build, the run stops after the load |
| `rebuild` | `clean` plus `build` |
| `wipe` | delete the whole dep dir of the target and clone it again. It does not build |
| `dirty` | mark a target and every dependent of it for rebuild |
| `deploy` | run the PAPA deploy stage, which gathers the libs, headers and assets |
| `upload` | deploy, then publish the archive to artifactory |
| `serve` | `rebuild` plus `update` plus `deploy` plus `upload` |
| `configure` | force a cmake reconfigure. It implies a build, so the target rebuilds after it |
| `test` | run the `test()` hook of the target |
| `start=<args>` | run the `start()` hook of the target |
| `open=<tgt>` | open the IDE project of a target |
| `unshallow` | convert a shallow clone or a shim into a full clone |

**Every action that names a target executes that subtree alone**, not only build, upload and deploy.
An out-of-scope dep builds nothing, and it must not reach packaging either, where a mamafile asserts
on libs that no run produced.

`clean` with no `build` returns straight after the load. A clean deleted the build dirs, so a
packaging pass would fabricate an empty package or fail a mamafile assert. `rebuild` sets `build`, so
it continues.

`clean all` also sweeps orphaned build dirs from disk. The tree walk cannot reach a dep whose source
is gone, so the sweep enumerates the workspace instead. It deletes only a dir that carries a mama
marker file, and only a dir belonging to this config.

## 5 Flags

**Target selection**: `target=<name>`, `all`, a bare word, `deps_only`.

**Platform**: `windows`, `msvc`, `linux`, `macos`, `ios`, `android`, `android-<N>`, `ndk-<ver>`,
`raspi`, `raspi32`, `mips`, `oclea`, `xilinx`, `imx8mp`.

**Arch**: `arch=<a>`, and the shorthands `x86`, `x64`, `arm`, `arm64`, `aarch64`.

**Compiler**: `clang`, `gcc`, `fortran`, `fortran=<path>`.

**Build config**: `release` (default), `debug`, `jobs=N`, `flags=...`, `with_tests`.

**Artifactory**: `art` forces a fetch and fails without one. `noart` skips every fetch.
`if_needed` skips an upload when the archive already exists.

**Diagnostics**: `sanitize=<list>`, `asan`, `lsan`, `tsan`, `ubsan`, `clang-tidy`, `coverage`,
`coverage=<opt>`, `coverage-report`, `coverage-report=<src_root>`, `buildstats`.

**Loading**: `parallel` (the default), `serial`, `parallel_max=N` (default 20),
`git_timeout=<seconds>`, `unshallow`, `https-override`, `ssh-override`.

**Also accepted**: `reclone` is the deprecated spelling of `wipe`. `start` and `open` take no argument
in their bare form. `sched_debug` prints the build-weight calculation and builds nothing.

**Caching**: `nocache`, also spelled `no-compiler-cache`, disables the cmake compiler seed.
`globalcache` moves the seed to the user cache dir, so one probe serves every checkout on the machine.

**Output**: `silent`, `verbose`.

**Testing**: `test`, `test=<args>`, `test_until_failure`, `test_until_failure=N`.

**Install utilities**: `install-clang-<ver>`, `install-gcc-<ver>`, `install-msbuild`,
`install-ndk-<ver>`, `install-raspi`, `install-raspi32`.

## 6 Dependency sources

A mamafile names its children in `dependencies()`. Three kinds exist.

**`add_git(name, git_url, git_branch=, git_tag=, git_commit=, mamafile=, shallow=True, args=)`** clones
a repository.
A tag is immutable by convention. A commit pin goes in the tag field. Mama routes a hex string to the
commit path. `args` reach the build dir name and the archive name, so one repository can serve two
consumers with different options without either overwriting the other.

**`add_local(name, source_dir, mamafile=, always_build=, args=)`** builds a directory of this
repository. It has no commit of its own, so its version comes from its source content.

**`add_artifactory_pkg(name, version=, fullname=)`** names a prebuilt archive. It has no source dir.
Its url is mandatory, so a 404 is fatal for it. `_should_build` marks it for rebuild, but a dep loaded
from artifactory has no build work, so nothing compiles.

A dep reached through two parents is one instance. `add_child` de-duplicates by name under a lock, so
a parallel load cannot create two. A second parent that passes more args recomputes the variant
suffix, and therefore the build dir.

## 7 The load

A load resolves one dep. It gets the source or the package, then parses the mamafile. It runs
`settings()` and `dependencies()`, so the graph learns the children.

### On-disk states of a non-root git dep

1. **Valid shim** - the `mama_shim` marker and `papa.txt` exist in the build dir, and the products are
   extracted. The steady state of a dep satisfied from artifactory.
2. **Stale shim** - the marker exists, but upstream advanced. Only ls-remote can tell.
3. **Real clone** - a `.git` directory exists. The dep builds from source.
4. **Empty** - nothing on disk yet.

### What each state costs

| State | `build` | `update` | `build noart` |
|---|---|---|---|
| 1 valid shim | load `papa.txt` from disk. No network, no unzip, no ls-remote | ls-remote, then re-fetch and re-extract | marker dropped, clones and builds from source |
| 2 stale shim | not detected, and that is deliberate | the probe re-extracts and rewrites the marker on a hit. On a miss the marker goes and the dep clones | marker dropped, clones and builds from source |
| 3 real clone | a local repo-health check, no network | fetch or pull, and reset only when the status moved | same as `build` |
| 4 empty | ls-remote, probe artifactory, else clone | same | clone |

Under `mama update <X>` only X takes the `update` column. Every other dep takes the `build` column,
because `check_status` runs only for the current target. A real clone that never built also runs the
post-clone artifactory probe, whatever the command.

A plain `mama build` makes no network call for a dep in state 1.

**Why:** `mama build` is the hot path. It runs many times per developer per day, so it has to stay
cheap. An ls-remote per shim across N deps is exactly the cost the cached path exists to avoid.

### Mama protects local modifications

A package under `packages/` is a working copy a developer edits, builds and tests in place before
publishing it. So a load protects it:

- Source on disk with no usable `.git` is never cloned over. Mama warns, builds it as-is, and names
  `mama wipe <target>` as the only way to discard it.
- `mama update` on a dep with uncommitted changes to tracked files fails loudly, before the pull that
  would overwrite them.
- A plain `mama build` runs no pull at all, so it cannot move a working tree the developer is using.
- An in-place edit still rebuilds the dep, through the working-tree fingerprint of reason 9.

One gap: the dirty-tree guard asks `git diff --quiet HEAD`, which reads a local COMMIT as clean. The
`reset --hard origin/<branch>` of an update then moves the branch off that commit. Uncommitted work is
safe. A commit the developer has not pushed is not.

**Why:** this is the workflow mama exists for. A tool that silently reset a package to origin would
lose a day of work, and no speed gain pays for that.

### The two-stage targeted load

A run that names one target loads its subtree and nothing else.

**Stage one, `load_path_to_target`**, reads the cheapest dep next and stops the moment the graph names
the target. A local dep costs one mamafile parse, so it reads first. A branch the walk never enters
stays unread. **This walk is serial on purpose**: a parallel wave would read the branches the early
stop exists to skip.

A **skim** names the children of a dep and nothing more. It parses the mamafile and runs `settings()`
and `dependencies()`, because only those two hooks name a child. It fetches nothing, clones nothing
and creates no build dir. While a skim runs, `build_dir()` and `source_dir()` raise, so a mamafile
that reads a path too early fails fast instead of writing outside the dep.

Both hooks run once per dep, and `did_skim` is what stops the later load from repeating them. A second
`dependencies()` call makes `add_child` refuse a child it already holds. A deferred load that parsed a
parent-supplied mamafile names children of its own, so `revive_deferred_load` drops them before the real
load runs the hook again.

Inside stage one, `_defer_load` skips every network step of a dep outside the target: the shim probe,
the package fetch and the clone. **Exploring the graph must never turn a cached shim into a clone.** A
deferred dep keeps its name, so `find_dependency` still finds it.

**Stage two, `revive_deferred_target_deps`**, loads the subtree of the target and nothing else. When
the graph never names the target, the cached packages expand first, because they cost no network. Only
then do the deps that need a fetch expand.

### The walk

`load_dependency_chain` walks the graph. Three arrivals at a dep are possible.

- The dep already loaded in an earlier walk. The walk returns at once and enters nothing.
- Another thread owns the load right now. This thread waits for the answer its own `after_load` needs,
  and it walks no children and draws no line.
- Nobody owns it. This thread owns the load, the subtree and the one display line.

One lock covers the claim alone. The load runs outside it, so different deps stay concurrent.

**The walk always enters the dep it starts from**, even when that dep already loaded. mamabuild loads
the root before the walk, and a reload revives deps below a scope that already loaded. A reload starts
one walk per revived dep, because a walk from the scope stops at the first loaded dep above it.

`after_load` propagates a rebuilt child up to its parent, and only on a run that named no specific
target. A targeted run scopes itself instead, through `mark_unbuilt_target_deps`. A shim has no
source, so a changed child cannot change what it produces, and a shim never inherits the flag.

## 8 Artifactory

### The archive name

```
{name}-{platform}-{os_major}-{compiler}-{arch}-{build_type}{variant}-{version}
```

`build_type` is `release` or `debug`. A download names the type the run asks for, because that name picks
the package to fetch. An upload names the type the `CMakeCache.txt` of the build dir records, because that
is the type the artifacts carry. The two share one build dir, so a run that uploads after a debug build of
another type would otherwise publish debug artifacts under the release name. A dir with no cache falls back
to the type of the run. `arch` is the arch marker, which an `-march` pin renames (`x64v3`, `armv82a`),
because a pin already names the architecture and one axis gets one field. `variant` is the same suffix the
build dir carries. For a git dep the version
is the first of: the mamafile `self.version`, the pinned git tag, or the commit hash. A hex tag is a
commit pin, so it counts as the hash. The name carries the first 7 characters of the hash, whatever
length the resolver answered. A branch pin labels the hash and does not replace it. A branch moves, so
its name alone would serve every commit ever pushed to it.

The other sources name themselves differently. The root always uses its checkout commit. A package dep
uses its declared version. A local dep uses its content version.

An `add_artifactory_pkg` with a `fullname` uses that name verbatim.

### The shim

A shim lets mama satisfy a dep without cloning it.

**Why:** a clone can be slow, and a consumer that only needs the built artifacts never needs the
source. The shim trades the clone for one archive download.
 `try_load_artifactory_shim` resolves the commit hash
without a clone: the cached `git_status` answers first, then a hex tag pin, and ls-remote last. It
fetches the archive named by the pinned version when a readable mamafile pins one, else by that hash.
It extracts the archive into the build dir and writes the `mama_shim` marker. It prints
`SHIM FETCHED <archive>`.

A later run finds the marker and takes `try_load_cached_shim`, which prints `SHIM CACHED <archive>`.
Specifically not `SHIM FETCHED`, which would claim a fetch that did not happen, and not
`Artifactory cache <path>`, which would claim a zip mama never touched.

`noart` never reaches the cached path. It forces the source clone instead, so the marker goes and the
git path clones. `update` also skips the cached path, and its regular probe re-extracts.

**The shim probe runs only when there is no working tree.** For an already-cloned dep the regular
fetch and reset path is correct, and the probe would only re-clone into a tempdir for nothing.

A **failed ls-remote does not drop the marker.** A transient network failure must not force a re-clone
on the next run.

Under `update`, a probe that finds no package drops a marker whose commit upstream has left behind, so
the dep clones and builds from source. A missing package for an UNCHANGED commit keeps the shim,
because the files it already extracted are still the right ones.

A dep that pins no version mama can read locally gets a second probe when the first one missed. The pin
may live in the dep's own not-yet-cloned mamafile. Mama sparse-fetches that one file, reads the pin,
and probes again under the pinned name. A dep that already resolved a pin skips this: a re-probe by
hash after a version pin would resurrect a stale archive.

### The zip cache

`artifactory_fetch_and_reconfigure` reuses a cached `<archive>.zip` in the dep dir, **unless
`config.update` and the dep is the current target**. `mama update` with no target rewrites the target
to `all`, so under it every dep re-downloads. Under `mama update <X>`, only X re-downloads and every
other dep reuses its cached zip.

**A download that answers nothing falls back to the cached zip the run skipped.** Only an `update` of the
current target skips that zip, so only that path can reach the fallback. One archive name holds one
package, so the copy on disk is the package the run wanted. An offline `mama update` therefore loads from
the cache instead of ending the run. An `add_artifactory_pkg` dep raises only when no usable zip is left.
A cached zip that fails to unzip is deleted, so the fallback never serves a corrupt package.

### A 404

**A 404 for a git dep is normal.** It means no prebuilt package exists for the current commit. It must
NOT wipe the `git_status` file. A wiped status makes the next `check_status` report an SCM change and
force a full rebuild. `check_status` already detects a real url, tag, branch or commit change by
direct comparison.

**A 404 is fatal for an `add_artifactory_pkg` dep.** Those urls are mandatory.

### Which runs may fetch

`can_fetch_artifactory` refuses for the root and for a dep already checked. Then:

- `noart` refuses, and it beats `art`. The post-clone probe says so, and the pre-clone shim probe
  refuses silently, because it asks with printing off. `noart` also refuses a shim already on disk,
  so every git dep builds from source.
- **An `add_artifactory_pkg` dep ignores every flag.** It exists only as a package, so `noart`, a
  rebuild and a clean cannot apply to it. It is read-only too, and it refuses a deploy and an upload.
  A missing network still refuses the download, because no flag can conjure the package.
- A `rebuild` of the current target refuses, because the source build is the point. Verbose only.
- A `clean` of the current target refuses, because the clean deletes the result. Verbose only.
- `art` forces the fetch for every dep the two rules above did not already refuse. A miss on a git dep
  still falls back to a source clone. Only a package dep raises on a miss.

## 9 Versions

`self.version` in a mamafile names the artifactory package. Both the download side and the upload side
must construct the same name, and the download side has to do it **before the clone**. So mama reads
the version out of the mamafile text without running the file.

Only one shape is trustworthy: **exactly one `self.version = '<literal>'` assignment** the reader can
resolve. A module-level `NAME = '<literal>'` binding resolves too. Two assignments mean the value
depends on which branch runs. A computed value stays invisible. In both shapes the reader would name a
package the upload side never publishes. So mama refuses the pin, and warns once per dep per run.

An unpinned **local** dep has no commit of its own, so mama names it by its source content.

### version_suffix

`add_git`, `add_local` and `add_artifactory_pkg` each take `version_suffix`. It appends to the resolved
version, so `caf5158` becomes `caf5158-2`. The suffix always passes through `sanitize_version`, which the
version itself only does on the tag and the branch paths.

`add_artifactory_pkg` refuses `fullname` and `version_suffix` together. A `fullname` names one exact
archive and returns before any version is composed, so a suffix beside it would go nowhere.

**Why:** the archive name covers the source and the toolchain, and nothing in it covers the packaging
recipe. Change `package()` in a mamafile and every already-published archive keeps serving the old
content. One machine can only rebuild the variants it can compile, so the rest stay stale. A suffix
renames the package on every platform and compiler at once, which is the only lever that reaches them.

**The parent declares it, never the target.** A target-side field would be invisible to the pre-clone
shim probe, so the download and the upload would name different archives.

A `V <dep> <suffix>` record in `papa.txt` carries it to a consumer, because a `D` record ends in a
variable-length arg list and cannot hold another field. Without that record a consumer of a package
would resolve the child by its unsuffixed name. A reader that predates the record sees no suffix.

The upload refuses to publish a name the download side cannot construct. A non-root local dep takes a
different rule, because its source is on disk for both sides and its mamafile may compute the version.
Only an uncommitted edit stops it, because no other machine can rebuild that tree.

## 10 Rebuild decision

`_should_build` decides for a `build` or `update` run, in this order. The first match wins. Reasons 3
to 15 print their reason unless the run is `silent`. Reasons 1 and 2 are silent skips, not build
reasons. `deps_only <X>` overrides the whole table and forces a rebuild on every dep of X.

1. An artifactory shim never builds. It has no source. `mama unshallow` converts it first.
2. A run that named another target skips this dep here. A dep has no parent link at load time, so
   `mark_unbuilt_target_deps` runs after the load. It marks the deps below the target that have
   nothing usable on disk.
3. A cleaned target builds.
4. `configure` on the target builds.
5. The root always builds.
6. `always_build` builds.
7. A changed git commit builds.
8. An `add_artifactory_pkg` dep builds.
9. A git dep with a real clone builds when its working tree changed. This is a fast fingerprint, not a
   reconfigure.
10. A local dep builds when its own subfolder changed, by the same fingerprint.
11. `update <X>` and `build <X>` build X.
12. A recorded build product that is now missing builds.
13. A dep with no build products builds, unless it came from a package or declared `nothing_to_build`.
    With no build files it reports `not built yet`, with build files `no build dependencies`.
14. A removed dependency changes the link list, so it builds.
15. A modified `mamafile.py` or `CMakeLists.txt` builds. An artifactory dep skips this check.

Otherwise the dep is up to date and reports `OK`.

After the load, `after_load` propagates: a dep whose child rebuilt also rebuilds, because a relink
needs the new lib. It runs only on a run that named no specific target, and never for a shim.

A configure does not propagate. The configure fingerprint hashes `mama-dependencies.cmake`, so a
rebuilt child whose exports did not change leaves the fingerprint equal, and its parent skips cmake.

## 11 Configure decision

`run_config` must configure when the run says `update` or `configure`, when `buildstats` runs on clang,
or when the sanitizer flags differ from the recorded ones. Then:

**Wipe, never soft-reconfigure, a build dir whose toolchain moved.** cmake keeps stale cache variables
such as `CMAKE_SYSTEM_PROCESSOR`, which mis-drive the project's own CMakeLists. A recorded toolchain
fingerprint that differs proves the move. A dir that predates fingerprints falls back to comparing its
cached compiler path.

**Wipe a build dir left half-configured by a killed configure.** A truncated or unreadable
`CMakeCache.txt`, a cache whose generator wrote no build file, or a partial compiler-detection dir all
poison the run. The detection check runs even with no cache at all, because a kill mid-detection often
saves none.

Else, with a valid `CMakeCache.txt` and no must-configure, mama skips the configure and adopts the
toolchain fingerprint as the new baseline.

**`update` asks for a configure per target, and mama compares the inputs first.** The cmake run is
skipped when the cache is valid, and when the recorded fingerprint still matches.

**Why:** a warm configure of a real project still costs most of a minute, and `update` would pay it
once per target for nothing. That fingerprint covers the toolchain, the build type, the cmake
defines, the install prefix, the source dir and the exports of the dependencies. `mama configure` is the explicit override, and it
never lands there.

**A multi-config generator also gets the set of configurations it may offer.** Visual Studio, Xcode and
Ninja Multi-Config read `CMAKE_CONFIGURATION_TYPES` and ignore `CMAKE_BUILD_TYPE` at build time. Mama
names two: the type this target builds, then `Debug`, or `RelWithDebInfo` when the target builds
`Debug`. The build itself still passes `--config` for the type of the target.

**Why:** the cmake default set adds `Release` and `MinSizeRel`, which mama configures for no
dependency. An IDE listed four configurations, and three of them could not link.

### The MSVC runtime library

Every cmake build on MSVC links the release CRT, whatever the build type. The configure command line
sets `CMAKE_MSVC_RUNTIME_LIBRARY=MultiThreadedDLL`, and it carries `-D_ITERATOR_DEBUG_LEVEL=0` inside
the C++ flags of a target that builds C++. It also sets `CMAKE_POLICY_DEFAULT_CMP0091=NEW`, because
cmake reads the runtime library under that policy alone, and a project below cmake 3.15 does not select
it. The generated `mama.cmake` also forces the same two on every consumer that includes it. The msbuild
path passes no runtime property, so a `.vcxproj` keeps its own.

**No target takes a runtime library of its own.** A mamafile that names `CMAKE_MSVC_RUNTIME_LIBRARY`
loses it, because mama appends its own after `cmake_opts` and the last `-D` on the line wins. A
mamafile that names a different CRT gets a warning. The same CRT is only redundant, so it stays quiet.

**Why:** one CRT and one iterator level across the tree is what lets a Debug root link Release
dependencies. A big app then stays debuggable and still runs fast enough to profile. A target that
diverged would fail the link with `LNK2038`. Across a DLL boundary, where no linker compares the two,
it would free a pointer on the wrong heap. CMP0091, NEW since cmake 3.15, moved the runtime library out of
`CMAKE_<LANG>_FLAGS_<CONFIG>`, which is what the `mama.cmake` rewrite reads. A project that holds the
policy at OLD reads no runtime library at all, so the rewrite is what reaches it. A third-party project
includes no `mama.cmake` at all, so the command line is the one route that reaches both.

**A mamafile that forces a C++ standard also gives cmake that standard.** `enable_cxx20()` and its
siblings write a compiler flag, `-std` or `/std`. cmake passes that flag through and never reads it,
so cmake believes the project named no standard. Mama maps the flag back to a number and passes
`CMAKE_CXX_STANDARD` and `CMAKE_CXX_EXTENSIONS`. A `gnu++` spelling sets the extensions ON. Every
other spelling sets them OFF.

The mapping reads the flag alone, never the build args. An arg the mamafile ignored therefore cannot
steer cmake away from the flag the compiler gets. An operator who passes a standard through `flags=`
wins over the mamafile, because cmake appends its own flag last. `c++latest` maps to no number,
because it names no fixed standard. Three more cases get nothing:

- a mamafile that forces no standard
- a mamafile that named the same variable through `add_cmake_options()`, in any spelling
- a cmake older than the release that learned that number

Mama never passes `CMAKE_CXX_STANDARD_REQUIRED`. A compiler that cannot give the standard has to
decay to the newest one it has, the way the flag alone already did. `REQUIRED` would turn that into a
failed configure, and the gate above reads the cmake version, never the compiler version.

**Why:** `target_compile_features()` and a `CXX_MODULES` file set both read the cmake standard, not
the compiler flags. Without this a C++20 project cannot use either, although every compile line
already carries `-std=c++20`. `EXTENSIONS` matters because cmake appends its own standard flag
after `CMAKE_CXX_FLAGS`. The cmake default would append `-std=gnu++20` after the `-std=c++20` mama
already passed, and silently turn on the extensions that flag leaves off. ON for a `gnu++` flag stops
the mirror of that: a strict `-std=c++20` appended after it, which refuses the extensions the source uses.

### The compiler seed

cmake re-runs compiler detection for every build dir it creates. Mama runs that detection once per
seed id, and injects the result into each build dir that holds no cache of its own.

The seed **transplants compiler detection only, never project flags**, so a single-language project
is not poisoned by the synthetic C plus C++ probe.

Detection writes some of its answers to the cmake CACHE alone, and a seeded configure skips it, so the
seed replays those lines from the cache of the probe. They are the ABI facts, the compiler and
toolchain entries, and every tool the binutils search found, such as `ar`, `ranlib` and
`clang-scan-deps`. The replay takes a closed set of keys, because one seed serves every target of a
compiler config, and a `find_program` result of one project must not reach another.

**Why:** that detection dominates a cold configure. `buildsys/cmake/compiler_cache.py` records the
measurement it was written from: about 6.5 seconds down to about 1.7.

The seed id carries the platform, the arch and a compiler hash, so one seed can never answer for
another platform. It lives in `<workspace>/.mama/compiler_seed` by default, so `rm -rf packages/`
heals a broken one. `globalcache` moves it to the user cache dir, where one probe serves every
checkout on the machine. A developer keeps the local root, because one bad seed in the user cache
would reach every project.

## 12 Build and packaging

A dep with real build work is one that is not header-only, not from artifactory, and flagged for
rebuild.

**CONFIGURE phase**: the `configure()` hook, then an automatic artifactory fetch, then the cmake
configure when that fetch found nothing. The whole phase is a no-op for a no-work dep, and for a
mamafile that overrides `build()`, which fuses configure and build and owns both.

**BUILD phase**: compile if there is work, then **always** package, so a no-work dep still publishes
its exports in dependency order.

A mamafile that overrides `build()` runs whole in the build phase. It reserves its cores from inside
`cmake_build()` instead of at launch, so the scheduler admits it without a reservation.

**Build parallelism**: the native build tool takes an explicit job count, and a target that keeps
`enable_multiprocess_build` always gets one. A dep takes the count its translation-unit probe sized,
and `config.jobs` when the probe found none. The root always takes `config.jobs`. msbuild takes
`/maxcpucount:N`, xcodebuild takes `-jobs N`, and make and ninja take `-jN`. A target that clears
`enable_multiprocess_build` passes no job count, except under ninja, which takes `-j1`.
**Why:** ninja with no flag reads the host core count. A container CPU limit does not bound that count.
A CI build then starts far more compilers than the limit allows, and the OOM killer stops it.

### Packaging

`_run_packaging` skips entirely when there is no build work AND no usable artifacts on disk. Wipe,
upload, deploy and test walk the chain without building, so they would otherwise package artifacts
never produced or just deleted.

The `package()` hook populates the exports through `export_include`, `export_libs`, `export_syslib`,
`export_asset` and `export_modules`. For a target built from source, each category the hook leaves
empty gets a default: includes, then libs and syslibs, then modules. A fetched dep runs no default.
`default_package()` runs the same three, so collecting the rest cannot widen a narrowed list.

**A fetched module list belongs to the include tree of the same run.** A hook that re-roots the
exported includes drops the module paths the archive recorded, and the module default finds them
again under the new roots. **Why:** a deployed module path sits under no source include dir, and
every consumer variable then loses it.

**A call to `export_modules()` decides, whatever it resolves to.** A recipe that names a module only
some platforms carry gets the empty result it asked for, never the automatic export. `papa.txt`
cannot refill that category either, so `no_export_modules()` holds for a fetched package too. `no_export_includes()`, `no_export_libs()` and
`no_export_modules()` opt one out.

`default_package_modules()` exports every module interface unit under the exported include dirs. It
runs after the include default, because a module ships inside an exported include tree or it cannot
ship at all. **Why:** a library that ships a module almost always publishes it.

`export_modules(path, [names])` narrows that list. A `None` name list globs every module extension
under `path`, and `recursive=False` reads that one directory alone. The call copies no file. The
include deploy carries the modules, because the copy admits every path the export named.

**An export decides which module files ship, and `include_glob_filter` cannot change that.** A
target that exports one module answers for every module file, so a filter naming a module suffix
ships no private module beside it. One module ships
exactly once, whatever order the hook used.

`strip_objects` sets a target-wide flag, and only an opt-out sticks. One
`export_modules(..., strip_objects=False)` keeps the module objects, whatever a later call passes.

### C++20 modules reach a consumer as source

A binary module interface is not portable, so a package ships the interface unit and the consumer
compiles it.

`mama-dependencies.cmake` writes `{name}_MODULES` and `{name}_MODULES_BASE_DIRS` per package, and
appends the module list to the aggregate `MAMA_MODULES`. The aggregate `MAMA_MODULES_BASE_DIRS`
takes the base dirs as literal paths. A dep that exports no
module writes nothing, so an upgrade reconfigures no existing project. The aggregate drops a base dir
that sits inside another, because cmake refuses a file set whose base dirs contain each other.

`mama_target_modules(target [scope])` in `mama.cmake` adds a `FILE_SET mama_modules TYPE
CXX_MODULES`, asks for `cxx_std_20`, sets `CXX_SCAN_FOR_MODULES ON`, defines `MAMA_HAS_MODULES=1`,
and names the modules it added in a cmake STATUS line. **Why:** cmake scans a source for `import`
only under CMP0155 NEW, which a consumer whose `cmake_minimum_required` predates 3.28 never gets. The scope is `PUBLIC` unless
the call names one. A library that installs itself through `install(EXPORT)` passes `PRIVATE`, because
cmake refuses to export a target whose `PUBLIC` file set it does not install. The consumer source
reads `#ifdef MAMA_HAS_MODULES` to import or to include the header.

**Every `#include` of a consumer must precede its first `import`.** A module makes the declarations
of its own included headers reachable, so a header parsed after the import re-declares them. GCC 14
rejects that as a redefinition of a standard entity. Mama cannot order the includes of a source it
does not own, so this is a rule its consumers follow.

`MAMA_MODULES_AVAILABLE` is the one gate, and every part must answer:

| Part | What it takes |
|---|---|
| the lever | `MAMA_ENABLE_MODULES`, an option that defaults to ON |
| cmake | 3.28 or newer |
| generator | Ninja 1.11 or newer, or Visual Studio 17 2022 or newer |
| compiler | GCC 14, Clang 18, or MSVC 1934 |
| clang | `CMAKE_CXX_COMPILER_CLANG_SCAN_DEPS` that exists, and no Visual Studio generator |

Mama measures ninja once per run and writes that number into `mama.cmake`, so no configure spawns
the tool to ask again. **Why:** a toolchain that misses one part keeps the exported headers, so a
build never fails because a compiler cannot read modules.

**Mama knows the floor, and no package declares one.** The three `MAMA_MODULES_MIN_*` values are
cache strings a consumer on an odd toolchain can move. An empty one refuses, and never passes.
`MAMA_ENABLE_MODULES=OFF` keeps the exported headers whatever the toolchain can do. **Why:** a floor
per package weighed a list `mama_target_modules` then took all or nothing, so it computed one
maximum the long way around.

`MAMA_HAS_MODULES=1` is one define for the whole target, so a consumer cannot import from one
package and read the headers of another. A refusal prints a cmake STATUS line.

A failing `package()` names its target and stops the run. Two runs carry on instead, and report the
gap. A `list` run builds nothing, so a `package()` that reads a build product cannot pass there. A
fetched dep already holds its export list in `papa.txt`.

### Rules against list, and why a fetched dep runs the hook

Two different things describe a package, and only one of them survives an archive.

- The **rules** live in `package()`: the include filter, the `as_includes_root` alias, and the
  `no_includes` and `no_libs` opt-outs. They decide WHICH files ship and WHERE they land.
- The **list** lives in `papa.txt`: the include dirs, the libs, the syslibs and the assets an archive
  holds. It decides WHAT a consumer links against.

`papa.txt` records the list and never the rules. So **a dep fetched from artifactory runs `package()`
too.** A deploy that skipped the hook fell back to the default header filter and shipped the wrong
files.

Mama merges the two per category. The hook owns every category it declares. `papa.txt` owns every
category the hook leaves alone, so a recipe that exports includes only keeps the libs the archive
recorded. A hook that declares nothing, or one that raises, leaves the whole recorded list standing
and the run carries on. **The default packaging never runs for a fetched dep**, because the archive
already holds an authoritative list and a glob would only guess at it.

### Packaging is idempotent

A package that came out of an archive must deploy the same tree the source build deployed. Anything
else means a republished package loses files, or grows them, on every trip.

Two fetched shapes have to agree. A **shim** has no working tree, so an export that names a source
path fails and the recorded list stands. A **fetched clone** still has its source, so the same export
succeeds and re-declares that category from source. Both produce the deployed tree of the source
build, because the archive was built from that same source.

**An unpacked archive is already rooted.** `as_includes_root` re-roots the tree it exports, so applying
it to the `include` dir an archive unpacked would nest that tree one level deeper per republish. A
deploy therefore copies an unpacked archive's include tree as it stands, and only a tree outside the
build dir still gets the alias. A source build always gets it.

`tests/test_papa_roundtrip/` pins all of this over every export style, both fetched shapes, and a
second reload.

A shared dep contributes its libs **once**, not once per path through the graph. The link order stays
Unix order: every lib appears after everything that references it.

## 13 Deploy and upload

Deploy runs only under `deploy` or `upload`. Inside those, it runs for the root with no named target,
for every dep under `all`, or for the one named target.

A target that sets `deploy_after_build` also deploys right after a build that did real work. A cached,
header-only or artifactory-loaded target deploys nothing, because it produced nothing new. The hook runs
at most once per run, so a run that builds and uploads does not deploy twice.

**Why:** a dependency ships a shared library, and the consumer needs that runtime beside its binaries
before a test starts. Windows has no RPATH, so one missing DLL aborts a test before its first line.

The build summary prints one line for what the deploy of the **current target** wrote: `Deployed 2
includes, 3 libs to <dir>`. The counters record only inside that hook, so the deploys of 30 other deps
never answer for the target the user named. A hook that deploys another target's package counts, because
the current target asked for it. The per-target `PAPA Deployed` lines stay in the build log.

**A shim never deploys or uploads.** It is read-only, its papa.txt and its unzipped tree must survive,
and the artifactory already holds that package. Mama says so and points at `mama unshallow`.

A target that sets `nothing_to_upload()` in `settings()` skips the upload, and the run says so. An
application at the root of a project publishes no package, so an upload of it has nothing to send. Without
that flag mama demands a `papa.txt` and fails the run. The deploy hook still runs, because a project that
deploys its runtime tree but publishes no archive is a normal shape.

`papa.txt` records, one per line:

| Record | Meaning |
|---|---|
| `P` | project name |
| `C` | the compiler that built it |
| `O` | what the objects are: build type, platform, arch, then the variant tokens and any `-march` pin |
| `V` | the `version_suffix` a parent declared for one dependency |
| `D` | a dependency source |
| `I` | an exported include dir |
| `M` | an exported C++20 module source |
| `L` | an exported lib |
| `S` | an exported system lib |
| `A` | an exported asset |

The `O` record reads `O debug linux x64 asan lgpl`. It answers what a consumer needs before it links:
the type, the target and every variant axis. A fetched package holds no `CMakeCache.txt`, so the record
is the only thing that names its build type, and the mixed-type warning reads it. A package written
before the `O` record carries no attributes at all.

An `-march` pin follows the arch as `march=x86-64-v3`, its real value. The build dir name and the archive
name carry the merged marker `x64v3` instead. The record is text, and a reader compares it against a CPU.
The parser splits the record on whitespace and searches it, so a new attribute needs no fixed place.

An `M` record holds one package-relative path, inside the include tree an `I` record already names.
**A deploy writes `M` records for the modules of its own target, never for a child's.** The `D`
record loads the child package, which carries its own modules. Two copies would make a consumer
compile one module twice, which cmake refuses.

A module under no exported include path ships nothing, and the packaging step warns. So does a
module outside the subtree `as_includes_root` deploys, because the copy carries only that subtree. The upload refuses an
`M` record whose file the include filter dropped, because the consumer would compile a source the
archive does not carry. A package written before the `M` record carries no module.

**The packaged static library loses its module objects.** A module interface unit emits a strong
`initializer for module X`. The consumer compiles the same source, so a whole-archive link of an
unstripped package finds two definitions. The strip removes each member named after a module the
target compiled. The modules of every package below it count too, because `mama_target_modules` compiles the whole
dependency tree into this target.

An archiver lists the path it stored, so each module takes the members sharing the most trailing path
components. MSVC drops the module extension, so a module that shares none falls back to its bare
name. A member stays whenever its name is ambiguous. That covers a non-module source of the same name, a
copy the exported modules cannot account for, and a module of that name this target does not export.
**Why:** removing the object of a private unit loses its definitions, and every consumer then fails
to link. Both the
listing and the removal go through the archiver of the platform. Windows, Android and every prefixed
cross platform name a full path there, because the host keeps that tool off PATH. A GNU thin archive names each member by a path, so it keeps its
module objects and says so.

The strip touches the packaged copy alone, so the producer's own binaries still link.
`export_modules(..., strip_objects=False)` keeps the objects, which a target whose own sources import
its own module needs. **An exported module must define nothing but its own interface**, because the
strip removes whole objects. A unit that defines a non-inline function loses it for a consumer on the
header fallback, so every strip warns.

**The exported library is the stripped one.** A consumer that builds this dependency from source
links `exported_libs` directly. The packaging step points it at a copy under `mama-nomodules/` that
carries the same file name. The original stays where `export_lib` found it, because the binaries of
that target need those objects. A later run reads that original again, never the copy the run before
recorded. An archive that compiled no module keeps its own path, and a fetched package is already
stripped, so both copy nothing.

A package declares no compiler floor. Mama knows the versions that build a module, and the consumer
moves `MAMA_MODULES_MIN_*` or sets `MAMA_ENABLE_MODULES=OFF`. See the consumer section of 12.

A package built by a different compiler than the current build warns and does not fail.
Compiler-scoped build dirs make a cross-family mismatch unreachable in practice. A compiler version
upgrade inside one family still reaches the warning, because the stamp carries the version. A package
written before the `C` record carries no stamp at all.

`papa_deploy` refuses any directory that holds a `mama_shim` marker, so a shim can never be
republished as if it were a source build.

`upload` with `if_needed` skips when the archive already exists on the server. The upload validates
the archive against `papa.txt` first, and rejects missing or unexpected content.

**A target that exports nothing publishes nothing.** `_run_packaging` marks it `no_upload` when the
packaging leaves no include, no lib, no syslib and no asset. `nothing_to_upload()` sets the same mark by
hand, and the automatic one never clears it. `validate_archive` refuses the same empty package as a
backstop, so no route publishes one.

A syslib-only package is NOT empty. Its zip holds only `papa.txt`, and that file carries the `S` records
a consumer links against, so it is worth publishing.

**Why:** such a package is worthless. A consumer fetches it, links nothing, and the run carries on as
though the dependency resolved. A docs-only or bundle-only target hits this by simply existing.

### unpublish

`unpublish=<selector>` deletes published archives of the target, then removes their local copies.

| Selector | What it names |
|---|---|
| `current` | the version this checkout resolves to |
| `<version>` | one version, on every platform and compiler |
| `prune-old[=N]` | every version except the newest N, default 20 |
| `prune-all` | every version of the target |

There is no bare `unpublish`: it raises and names the four selectors. `deps_only` refuses to combine
with it, because `deps_only` means act on the dependencies while the unpublish scope names the target,
and a delete must not guess between them. A `clean` run does unpublish. The clean takes the build dirs,
and the cached zips live one level up in `dep_dir`, so they are still there to remove.

A version selector matches **everything after the build type**, so one
selector reaches every platform, compiler and build type at once. A file whose name does not parse as
this target's archive is never touched. A `version_suffix` is part of that tail, so `caf5158-2` and
`caf5158` are two different versions, which is the point of the suffix.

**A version is as fresh as its freshest archive.** `prune-old` orders versions by the newest upload time
among their archives, so a rebuild of an old commit keeps that version alive. Upload time is what the
server knows. Nothing tries to order bare commit hashes.

**An undated version is neither pruned nor counted.** A server that refuses MDTM dates nothing, and
counting those against the keep window would push every real version out of it.

**`prune-old` never takes the version this checkout resolves to.** Housekeeping must not leave the tree
naming a package that exists nowhere. `prune-all` does take it, because a user who typed `all` asked for
exactly that.

**A real version beats a keyword.** A git tag may spell `prune-all`, so a selector that matches a
published version names that version and nothing else.

A variant such as `asan` sits between the build type and the version, and nothing in the name marks
where it ends. So a variant build reads as a version of its own and keeps a separate history.

**One prompt covers the whole run, not one per target.** `mama all unpublish=prune-old` asks a single
question and opens a single FTP session. Thirty questions is thirty chances to stop reading them.

**The prompt prints every archive with its date and size, oldest first, and names every local path the
run will delete.** Approving a list of server archives must not silently take an unpacked build dir too.
A headless run cannot answer, so it refuses unless the command line also says `yes`. Refusing beats
hanging a CI job on stdin.

**A confirmed unpublish also drops the local copies:** the cached zip of every archive that really left
the server, and EVERY build dir of that dep whose shim marker names one of them. Every platform, not
only the one this run builds: a shim left in another platform's dir would go on naming an archive the
server no longer has. A dir goes only when it holds no `.git` and differs from `src_dir`, because a dep
named after a platform build dir shares the two paths, and a remove there would take the working tree.

**A file name the server supplies is never a path.** The local purge deletes by that name. A name
holding a separator, a `..` or a drive is refused three times: at the listing, at the selector and at
the purge itself. A shim of a version this run kept
stays, and a zip whose delete failed keeps its cache, because the server still has it. The machine that
cleans the server must not keep serving what it deleted, which the cached-zip fallback of section 8 would
otherwise do.

**A run that matches no archive names every target it listed, its archive count and its version count.**
A `prune-old` report also names the count it keeps, which a bare `prune-old` leaves out. That selector
keeps versions, and one version holds one archive per platform. An archive count alone therefore reads
as far more than the selector spared. A run that reached no target says so, and a run whose targets are
all read-only packages says that instead.

**Why:** a bare `Nothing to unpublish` reads the same for a wrong target name and for a selector that
spared everything.

**The scope is the target the user typed, not `config.target`.** An `update` rewrites an empty target to
`all`, and an unpublish that followed it would delete every version of every dep from a command line that
named none. A bare word counts as typed, so `mama ReCpp unpublish=prune-old` reaches the same target as
`mama target=ReCpp unpublish=prune-old`.

An `add_artifactory_pkg` dep refuses to unpublish, because it is read-only.

## 14 Test, start and open

`test`, `start` and `open` need source on disk. On a shim each one refuses, says why, and points at
`mama unshallow <target>`. A refused test must not block `start`, which asks for itself.

**A target whose mamafile defines no `test()` reports a skip.** It must not claim a test run that did
not happen.

`test_until_failure` runs the hook in a loop until it raises, up to N iterations, default 100.

`open` finds the IDE project the platform's own generator writes: a `.sln` or `.slnx` file for Visual
Studio, an `.xcodeproj` directory for Xcode. The newest match wins, because a build dir configured by
two toolsets holds both formats and the stale one opens an empty solution. With no match it falls back
to VS Code.

## 15 Platforms

Every target platform is one class under `mama/platforms/`, reached through a single `config.platform`.
Build system logic lives in `mama/buildsys/`, never in a platform. No consumer chains over platform
names: what it needs, it declares on `Platform` and reads back.

See [docs/platforms.md](platforms.md) before you add a platform, add a compiler flag, or add a cmake
option.

### The instruction set

A platform names the `-march` its target needs, or none at all. A native platform names `native` when
the host arch IS the target arch, and the baseline of the arch otherwise. `native` compiles for the CPU
of the build machine, and on a foreign arch it would compile host instructions. A cross platform names
a fixed baseline (android, raspi, oclea, imx8mp) or nothing (xilinx, mips, ios). MSVC has no `-march`.

`config.set_target_march(arch, march)` overrides that default for one target arch, and only for the
run that builds that arch. It belongs in the ROOT mamafile `settings()`, which runs before any
dependency loads. The root and every dependency then compile with the same instruction set. It raises
on an unknown arch, and on a value that is not the `-march` value alone. A platform whose compiler has
no `-march`, which today is only MSVC, warns and keeps its default.

The pin REPLACES the platform default, so exactly one `-march` reaches the compiler. `config.flags`
goes on the line as a raw string that mama never merges, so a `flags=-march=...` still puts a second
one there. The first one is what `compile_commands.json` reports, and a shadowed pin misreports the
build.

**Why:** `-march=native` is the right default for a developer and the wrong one for a release. It bakes
the build machine into the binary. The failure then appears as an illegal instruction on the older CPU
of a user, far from the build that caused it.

## 16 Output

### The live display

Both schedulers draw one line per dep, redrawn in place on a terminal. A dep keeps one line across its
whole workflow, so load, configure and build share it and the summary shows every phase.

On a terminal, a dep that succeeded and whose every phase finished in under 0.1 seconds is hidden. A
pure cached no-op is noise, and the package listing below names every dep anyway. A failure always shows.

The root loads **outside** every display. Its `settings()` picks the toolchain, and a mis-picked one
must never hide inside a live region.

### A non-terminal run

A pipe and a CI runner redraw nothing, so mama reports each stage as it happens. A `>` line opens every
phase, and one summary line closes the dep when its last phase ends, or when any phase of it fails.
That run hides no dep, because every dep it opened must also close. It dumps the full output of a
failed dep, and of every dep under `verbose`.

**A phase that never finished still reports.** Closing the display dumps what each open phase buffered,
to the screen and to the log. A killed compiler and a stop signal both end a phase with no finish, and
those buffered lines are the only evidence left. A non-terminal run also flushes each line as it writes
it, because a killed process loses a block-buffered stream.

**A stop signal reports as an interrupt.** `SIGTERM` and `SIGHUP` raise the `KeyboardInterrupt` mama
already handles. A running build stops its children, and every exit path closes the display and drains
the log. The interrupt carries the signal name, so the report reads `stopped by SIGTERM`, not `Ctrl+C`.
The default action of `SIGTERM` ends the process at once and loses all of it. A CI cancel and a job
timeout both send it.

**A failed command names its exit status, and says when the child printed nothing.** A negative status
is a POSIX signal, so mama names that signal, not the bare number. It reports `SIGKILL` as the usual
mark of the kernel out-of-memory killer.

**Why:** a compiler the kernel kills writes no diagnostic at all. Without those two facts the run ends
with an empty log and a number the reader cannot decode.

**A download reports what failed, in one line.** Every download hands its caller a result and an error.
A timeout, an HTTP status, a missing `Content-Length` and a truncated body each name themselves. A
caller that cannot continue raises a `BuildError`, and mama prints that one line. The traceback prints
only under `verbose`, and it holds mama frames alone, because the download raises no urllib exception.

**A failed download keeps the cached file, and it leaves no partial one.** A transfer writes a `.part`
file and renames it over the cached name only after the last byte. Every other end deletes the `.part`
file, so a timeout, a truncated body and a stop signal all leave the cached archive whole. That archive
is the one the artifactory fallback loads.

**The timeout follows what the download is for.** A download mama can answer another way waits
`DOWNLOAD_TIMEOUT`, which is 5 seconds. The artifactory fetch is that kind, because a cached package or
a source build answers the same question. A file no cache can replace waits `REQUIRED_DOWNLOAD_TIMEOUT`,
which is 15 seconds. A GNU source archive, an NDK zip and a mamafile `self.download_file` or
`self.download_and_unzip` call are that kind. A `GnuProject` reads the wait back from
`self.download_timeout`, so a mamafile can raise it for a slow mirror.

**Why:** the timeout is the wait for the next byte, not a budget for the whole transfer, so a slow but
live download never trips it. A dead network pays the wait once per fetch already in flight. The first
failure marks the network unavailable, and every artifactory fetch that starts after it returns at once.
Five seconds keeps that cost near the build time of a small target. A server that sends nothing for 15
seconds has already dropped the request, so a longer wait buys no download.

### The build log

**mamabuild opens one log per run**, under the workspace the root named, after the root load. Every
phase writes to that one log, and a display never opens one of its own. The log holds the full output
of every dep as one contiguous block, never intermixed across parallel deps. It also holds the lines
no display owns: the banner, the build summary and the compiler diagnostics. The package listing
reaches it either as an ownerless line or inside the block of its own dep. The process
exit drains and closes it.

### Git output

Every git runner that streams output to the user routes it through one filter. The one-shot commands
that capture their output silently never reach it.

- A transfer counter (`Receiving objects: 42%`) collapses into one throttled redraw. A counter the
  table misses would reach the terminal once per percent.
- Transfer bookkeeping is noise outside verbose: `remote: Enumerating objects`, `remote: Total`,
  `From <url>`, a ref update, `Cloning into`, and the submodule reports.
- Mama detaches on purpose, so the two runners that check out a tree, `run_git` and
  `clone_with_filtered_progress`, prefix their command with `advice.detachedHead=false`.

## 17 Concurrency

Parallel load is the default for every run, and `serial` opts out.

- `parallel_max` (default 20) sizes the LOAD pool of the unified scheduler.
- The `fetch_slot` semaphore caps concurrent git fetches at 8, whatever `parallel_max` asks for. A shim
  probe takes one slot for its clone, and one more for the ls-remote that resolves the commit. Its
  `git show` reads the clone it already made, so it takes none.
- `ensure_master_for_url` sets up ssh multiplexing. It is idempotent and serialized per host.
- A host that pushes back turns on a connection pacer for the rest of the run.

**Locks**, from narrowest to widest:

- `BuildDependency._load_lock` serializes `load()` of one dep, so exactly one thread clones it.
- The walk claim gives one thread ownership of a dep's load, subtree and display line.
- `interprocess_dir_lock(dep_dir)` serializes the shim setup and the checkout of one dep **across
  processes**. Without it a sibling `mama <host> build` reads a half-written clone as a broken tree.
  The sidecar lives in `<workspace>/.mama/locks`, never inside the dep dir. `mama wipe` removes a whole
  dep dir, and a lock file unlinked while held stops excluding anything. A timed-out acquire still
  runs, unlocked, so the lock can never hang a build.

**Abort**: Ctrl+C, `SIGTERM` and `SIGHUP` all set a process-wide flag. Every phase gate closes, so no new work starts. A live
child gets a grace period to stop on its own, and only a child that ignores the request gets killed.
On a failed load, mama terminates the queued children before the exit, or the pool would run the
entire queued backlog of clones first.

## 18 Environment

| Variable | Meaning |
|---|---|
| `MAMA_ARTIFACTORY_USER`, `MAMA_ARTIFACTORY_PASS` | artifactory credentials, for CI |
| `MAMA_CACHE_DIR` | where the user cache lives. CI and the test suite point it at their own dir |
| `MAMA_GLOBAL_COMPILER_CACHE=1` | same as the `globalcache` flag, for a whole session |
| `NINJA` | path to the ninja executable |
| `ANDROID_HOME`, `ANDROID_NDK_HOME`, `ANDROID_NDK_ROOT`, `ANDROID_NDK_LATEST_HOME` | Android SDK and NDK |
| `RASPI_HOME`, `OCLEA_HOME`, `IMX8MP_SDK_HOME`, `XILINX_HOME` | cross toolchain roots |
| `CLANG_TIDY` | path to clang-tidy, when PATH does not hold it |
| `ANDROID_SDK_ROOT`, `ANDROID_NDK` | the other spellings mama accepts for the Android SDK and NDK |
| `<PLATFORM>_SDK_HOME`, `OCLEA_SDK`, `XILINX_SDK`, `RASPBERRY_HOME` | the other cross toolchain roots |
| `ANDROID_CLANG_TIDY` | path to clang-tidy for an android build |
| `CC`, `CXX` | override the host compiler |
| `VCPERF` | path to vcperf, for MSVC `buildstats` |
| `GIT_SSH_COMMAND` | mama sets it for ssh multiplexing, and only when it is unset |
| `LD_LIBRARY_PATH` | extra search roots for a system lib |
| `HOME`, `HOMEPATH`, `LOCALAPPDATA`, `XDG_CACHE_HOME` | where the workspace and the user cache default to |
| `CI`, `TF_BUILD`, `JENKINS_URL`, `TEAMCITY_VERSION` | any one of these makes mama treat the run as headless |

Mama also reads `PATH`, `USER`, `XDG_RUNTIME_DIR`, `WindowsSDKVersion` and
`PROCESSOR_ARCHITECTURE` / `PROCESSOR_ARCHITEW6432` as ordinary host facts.
