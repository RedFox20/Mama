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
| 13 | [Deploy and upload](#13-deploy-and-upload) | papa records, the upload guard |
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
    <build_dir_name>/                   one per platform, arch, compiler and variant
      CMakeCache.txt, papa.txt, mama_shim, git_status, mamafile_tag, mama_exported_libs
    <archive>.zip                        the cached artifactory package
```

`workspaces_root` is the root project dir, unless the root mamafile declares `global_workspace`, which
keeps it at the user home dir. Only the mamafile parse assigns it, so a root with no `mamafile.py`
also keeps the user home dir. See `docs/BUGS.md`.

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

**Every `(platform, arch, variant)` pair gets its own build dir, and Linux also splits gcc from clang.**
A shared dir means two builds clobber each other's cache and libraries.

**Debug and release deliberately share one build dir.** The name carries no build-type token, so
`mama build debug` reuses the dir `mama build` used. It runs no configure either, because a build-type
flip does not force one, so the cmake cache keeps the older `CMAKE_BUILD_TYPE`. See `docs/BUGS.md`.
The artifactory archive name does carry `release` or `debug`, so the two packages never collide on the
server.

**Why:** mama lets a project mix release and debug packages, so the build type belongs to the package
name and not to the tree. A sanitizer is the opposite case. `-asan` and `-tsan` each get their own
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
| 1 valid shim | load `papa.txt` from disk. No network, no unzip, no ls-remote | ls-remote, then re-fetch and re-extract | ls-remote to check staleness, then load from disk |
| 2 stale shim | not detected, and that is deliberate | the probe re-extracts and rewrites the marker on a hit. On a miss the marker stays and nothing clones | detected, marker dropped, clones and builds from source |
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
parent-supplied mamafile is the hole in that rule. See `docs/BUGS.md`.

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
the root before the walk, and a reload revives deps below a scope that already loaded.

`after_load` propagates a rebuilt child up to its parent, and only on a run that named no specific
target. A targeted run scopes itself instead, through `mark_unbuilt_target_deps`. A shim has no
source, so a changed child cannot change what it produces, and a shim never inherits the flag.

## 8 Artifactory

### The archive name

```
{name}-{platform}-{os_major}-{compiler}-{arch}-{build_type}{variant}-{version}
```

`build_type` is `release` or `debug`. `variant` is the same suffix the build dir carries. For a git dep the version
is the first of: the mamafile `self.version`, the pinned git tag, or the commit hash. A hex tag is a
commit pin, so it counts as the hash. A branch pin labels the hash and does not replace it. A branch
moves, so its name alone would serve every commit ever pushed to it.

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

`check_staleness` gates the ls-remote inside the cached path. It is True under `noart` and False under
a plain build. `update` skips the cached path entirely, so the regular probe re-extracts.

**The shim probe runs only when there is no working tree.** For an already-cloned dep the regular
fetch and reset path is correct, and the probe would only re-clone into a tempdir for nothing.

A **failed ls-remote does not drop the marker.** A transient network failure must not force a re-clone
on the next run.

A dep that pins no version mama can read locally gets a second probe when the first one missed. The pin
may live in the dep's own not-yet-cloned mamafile. Mama sparse-fetches that one file, reads the pin,
and probes again under the pinned name. A dep that already resolved a pin skips this: a re-probe by
hash after a version pin would resurrect a stale archive.

### The zip cache

`artifactory_fetch_and_reconfigure` reuses a cached `<archive>.zip` in the dep dir, **unless
`config.update` and the dep is the current target**. `mama update` with no target rewrites the target
to `all`, so under it every dep re-downloads. Under `mama update <X>`, only X re-downloads and every
other dep reuses its cached zip.

### A 404

**A 404 for a git dep is normal.** It means no prebuilt package exists for the current commit. It must
NOT wipe the `git_status` file. A wiped status makes the next `check_status` report an SCM change and
force a full rebuild. `check_status` already detects a real url, tag, branch or commit change by
direct comparison.

**A 404 is fatal for an `add_artifactory_pkg` dep.** Those urls are mandatory.

### Which runs may fetch

`can_fetch_artifactory` refuses for the root and for a dep already checked. Then:

- `noart` refuses, and it beats `art`. The post-clone probe says so, and the pre-clone shim probe
  refuses silently, because it asks with printing off.
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

### The compiler seed

cmake re-runs compiler detection for every build dir it creates. Mama runs that detection once per
seed id, and injects the result into each build dir that holds no cache of its own.

The seed **transplants compiler detection only, never project flags**, so a single-language project
is not poisoned by the synthetic C plus C++ probe.

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

### Packaging

`_run_packaging` skips entirely when there is no build work AND no usable artifacts on disk. Wipe,
upload, deploy and test walk the chain without building, so they would otherwise package artifacts
never produced or just deleted.

The `package()` hook populates the exports through `export_include`, `export_libs`, `export_syslib`
and `export_asset`. When it exports no includes, the default include packaging runs. When it exports
no libs and no syslibs, the default lib packaging runs. `no_export_includes()` and `no_export_libs()`
opt out.

A failing `package()` names its target and stops the run. A `list` run builds nothing, so a
`package()` that reads a build product cannot pass there. That is not a failure of the run, so a list
reports the gap and carries on.

Mama skips `package()` for a dep it fetched from artifactory, unless the run asks for a local rebuild.
The papa.txt of the package already holds the exports.

A shared dep contributes its libs **once**, not once per path through the graph. The link order stays
Unix order: every lib appears after everything that references it.

## 13 Deploy and upload

Deploy runs only under `deploy` or `upload`. Inside those, it runs for the root with no named target,
for every dep under `all`, or for the one named target.

**A shim never deploys or uploads.** It is read-only, its papa.txt and its unzipped tree must survive,
and the artifactory already holds that package. Mama says so and points at `mama unshallow`.

`papa.txt` records, one per line:

| Record | Meaning |
|---|---|
| `P` | project name |
| `C` | the compiler that built it |
| `D` | a dependency source |
| `I` | an exported include dir |
| `L` | an exported lib |
| `S` | an exported system lib |
| `A` | an exported asset |

A package built by a different compiler than the current build warns and does not fail.
Compiler-scoped build dirs make a cross-family mismatch unreachable in practice. A compiler version
upgrade inside one family still reaches the warning, because the stamp carries the version. A package
written before the `C` record carries no stamp at all.

`papa_deploy` refuses any directory that holds a `mama_shim` marker, so a shim can never be
republished as if it were a source build.

`upload` with `if_needed` skips when the archive already exists on the server. The upload validates
the archive against `papa.txt` first, and rejects missing or unexpected content.

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

## 16 Output

### The live display

Both schedulers draw one line per dep, redrawn in place on a terminal. A dep keeps one line across its
whole workflow, so load, configure and build share it and the summary shows every phase.

A dep that succeeded and whose every phase finished in under 0.1 seconds is hidden. A pure cached no-op
is noise, and the package listing below names every dep anyway. A failure always shows.

The root loads **outside** every display. Its `settings()` picks the toolchain, and a mis-picked one
must never hide inside a live region.

A non-terminal run (a pipe, or a CI runner) writes one summary line per dep instead, and dumps the
full output of a failed dep.

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

**Abort**: Ctrl+C sets a process-wide flag. Every phase gate closes, so no new work starts. A live
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
