# Roadmap: load-phase and config hardening (and possible scoped discovery)

**Status:** planning only. No code in this roadmap has been written. Items 1+ are proposals.
**Audience:** an engineer or model picking this up cold. This document is self-contained.
**Repo:** `RedFox20/Mama` (`mama/` package).
**Consumer that triggered the work:** a downstream ground-control project, generalized here as `GCS`.

---

## 0. How to use this document

Each roadmap item below is designed to be **landed alone** and leave the tree stable. Do not
batch them. Every item states its own goal, evidence, risks, test plan, done-criteria and
rollback.

Evidence is tagged so you know how much to trust it:

- **[V]** - verified directly by reading the code during this analysis. Cite-and-trust.
- **[R]** - reported by a sub-audit with a file:line citation, but not independently re-read.
  **Re-verify before acting on it.**

`file.py:NN` line numbers were accurate at commit `3b0c654`. `mama/build_dependency.py` is
under active concurrent development (see section 2), so **line numbers drift - always grep for the
quoted code, never trust the number alone.**

---

## 1. Why this work exists (origin story)

A GCS Android build failed with missing headers (`rpp/strview.h`, `otherdep/...`) in a
dependency that was supposedly present. Diagnosis chain:

1. `rpclib` needs a **host** `protoc` while cross-compiling to Android (an arm64 protoc
   is "Exec format error"). Its CMake does `find_program(PROTOC_EXECUTABLE protoc REQUIRED)`
   at **configure** time, so the path must exist before configure completes.
2. The bootstrap for that is a nested `mama <host> build target=protobuf` child process.
3. A **targeted** build takes the classic path and calls `load_dependency_chain(root)`, which
   loads/clones the **entire** graph - so the nested Linux child was re-cloning shared deps into
   the *same* source trees the outer Android build was compiling from.
4. Two mama **processes** writing the same git working trees corrupted them. The in-process
   `threading.Lock` at `BuildDependency._load_lock` does not span processes. **[V]**

That corruption is fixed (section 2). What remains is the *waste*: the nested child still loads the
whole graph to build one leaf. "Scoped discovery" (Item 4) is the proposal to fix the waste -
and it is now an **optimization, not a correctness fix**. Items 1-3 are hardening that stands
on its own merit regardless of whether Item 4 ever happens.

---

## 2. Current state

### Landed on `master`

| Commit | What |
|---|---|
| `9b31fac` | `fix:` wipe build dirs whose toolchain moved instead of soft-reconfiguring. Records a toolchain fingerprint per build dir, wipes on mismatch. Fixed Android `-march=x86-64-v3` leaking into arm64 after a nix NDK path change. |
| `0a3f149` | `feature:` `build_host_binary()` - `config.host_platform_name()`, `BuildTarget.host_build_dir()`, `BuildTarget.build_host_binary(relpath, auto_build=True)`, `config.root_source_dir`. Cheap-checks the host build dir, else bootstraps `mama <host> build target=<name>`. |
| `3b0c654` | `fix:` cross-process dep lock. `mama/utils/dir_lock.py` (`flock`/`msvcrt`, sidecar kept **outside** `dep_dir` so a reclone-wipe cannot unlink a held lock, OS-released so it cannot hang). Wraps shim+checkout in `_load`. |

`2d8f54b` (`guard package() on having artifacts...`) landed from another source between two of
these. **Expect more concurrent commits in `build_dependency.py`.**

### Not landed - consumer-side rewire (uncommitted, deliberate)

The downstream GCS project has an uncommitted working tree that deletes its own local
`ensure_host_protoc` helper (~50 lines, now redundant) and rewires its rpc module's
`_protoc_path()` to `get_dependency("protobuf").target.build_host_binary("bin/protoc")`.

That rewire requires mama >= `0a3f149`, but the consumer still pins an older rev, so the
consumer-side commit must wait for the pin bump or its CI breaks.
`requires_version('0.13.01')` accepts newer, so a `0.13.02` release satisfies it.

### Inbound from another model - **Item 0**

A patch is being written for: *`settings()` is not loaded correctly under the parallel loader.*

Relevant code, unchanged as of `3b0c654` **[V]**:

```python
# mama/build_dependency.py  (in _load)
target.settings() ## customization point for project settings
if self.is_root:
    conf.lock_compiler()  # root settings() is the last prefer_clang/gcc; lock before any dep loads
    self._update_dep_name_and_dirs(self.name)  # build_dir was computed pre-flip, re-resolve it
target.dependencies() ## customization point for additional dependencies
```

```python
# mama/dependency_chain.py  (in load_dependency_chain)
changed = dep.load()
if dep.config.parallel_load:
    futures = []
    for child in dep.get_children():
        futures.append(e.submit(load_dependency, child))
```

Sibling `settings()` therefore run **concurrently on different threads**, so any global
`config` mutation from a non-root `settings()` is a data race with nondeterministic ordering. **[V]**

**This is the same surface as Item 2.** Whoever lands Item 2 must rebase onto the settings()
patch and re-check that the two do not double-guard or contradict each other. Item 2 may
shrink to a subset once the settings() patch lands - that is a good outcome, not a conflict.

---

## 3. Analysis: what the load phase actually guarantees

Facts an auditor needs before touching anything.

### 3.1 Two execution paths

`_can_unify(config)` **[R]** (`mama/main.py:191-197`) picks the engine:

```python
return (not config.serial_load and (config.build or config.update)
        and config.no_specific_target() and not config.list and not config.deps_only
        and not config.dirty and not config.mama_init)
```

`no_specific_target()` = no target or `target=all`. **Therefore any `target=X` always takes the
classic `load_dependency_chain(root)` path.** Scoped discovery only ever concerns that branch.

`execute_unified` - used only for *non*-targeted builds - is **already an incremental loader** **[V]**
(`mama/dependency_chain.py:983-994`): `_do_load` loads one dep, then `grow()` adds jobs for its
newly discovered children under the scheduler lock. It never stops early. This is the machinery
Item 4 should extend rather than replace.

### 3.2 Unloaded siblings are zombies, not absences - **the pivotal fact**

`dependencies()` declares **all** of a target's children in one user function, and `add_child`
constructs each unconditionally **[V]** (`mama/build_dependency.py`, guarded by
`config.dep_registry_lock`):

```python
dep = self.config.loaded_dependencies.get(dep_source.name)
if dep:
    dep.update_existing_dependency(dep_source)   # merges args ONLY
else:
    dep = BuildDependency(self, self.config, self.workspace, dep_source)
    self.config.loaded_dependencies[dep_source.name] = dep
...
self.children.append(dep)
```

You **cannot** suppress a single child. So a skipped dep is a *zombie*: present in
`parent.children`, `target is None` (git sources), `children == []`.

Worse, `is_pkg` and `is_src` sources call `create_build_target()` **eagerly in `__init__`** **[V]** -
so a skipped local-source sibling has a **non-None target with empty
`exported_libs`/`exported_includes`**. It will not crash. It will silently emit empty
link/include variables. Silent-wrong beats loud-crash for damage.

Only one place currently handles the zombie state, and it was added after a real crash **[R]**
(`build_dependency.py:212`, pinned by `tests/test_target_scoped_build/...:185`):

```python
if self.target is None: return self.has_build_files()  # load failed/never ran: judge by the build dir
```

### 3.3 Child dirs are computable before parsing

A child inherits the parent's workspace **[V]** (`BuildDependency(self, self.config, self.workspace, ...)`),
and `_update_dep_name_and_dirs` derives `dep_dir = workspaces_root/workspace/name`. So a dep's
location is known **before** its mamefile is parsed. This is what makes a pre-clone probe
structurally possible.

### 3.4 A mamefile is readable without cloning *only sometimes*

`mamafile_path()` **[V]**:

```python
if self.mamafile: return self.mamafile                              # override, lives in the PARENT repo
if self.src_dir: return normalized_join(self.src_dir, 'mamafile.py')  # requires the clone
return None
```

So **free** expansion = `add_local` deps and `add_git(..., mamafile="...")` overrides.
**Expensive** = a plain `add_git` with no override.

### 3.5 An artifactory shim probe is NOT cheap - kills the obvious design

Only the commit-hash step is cheap (`git ls-remote`, 5s timeout). The **dependency list lives
in `papa.txt` inside the archive**, so obtaining it requires downloading the complete
`{archive}.zip` over HTTP and unzipping it into `build_dir` **[R]**
(`artifactory.py:283-290` -> `:311-314` -> `:252-280`). There is no range fetch, no `papa.txt`
endpoint, no manifest sidecar.

**Consequence:** "expand the frontier with shims until the target is found" - an intuitive and
previously proposed design - is the *most expensive* possible expansion, not a cheap peek.
Reject it. (Conversely this is where the *savings* are: every sibling skipped avoids a full
download + unzip.)

### 3.6 The fork bomb - a permanent invariant

`mark_unbuilt_target_deps` **[R]** (`dependency_chain.py:82-92`) revives deps that
`_should_build` skipped, **scoped to X's own subtree**. Its regression test states the reason
verbatim **[V]** (`tests/test_target_scoped_build/test_target_scoped_build.py:44`):

> *"The fork bomb, exactly: rpclib.configure() shells out to `mama build target=protobuf` to get
> a host protoc. If that child revives rpclib (it depends on protobuf, so it sits ABOVE it), the
> child re-enters the same configure() and spawns another child, forever."*

That is precisely the `rpclib` -> `build_host_binary` pattern. **Never revive ancestors of
the target.** `build_host_binary`'s `config.name() == host_platform_name()` early-return is the
second, independent guard on the same hazard. Both must survive every future change.

### 3.7 Name-only dedup, first declaration wins, silently

`loaded_dependencies` is keyed on **name alone**. A second parent declaring the same name with a
different url/branch/tag has those fields **discarded with no warning** - only `args` merge **[R]**
(`build_dependency.py`, `update_existing_dependency`). The first `add_child` also fixes `parent`,
hence `mamafile` and `src_dir` resolution.

Under full load, "who is first" is a stable-ish whole-graph traversal. Under **scoped** load a
different parent can win -> different clone -> different commit hash -> **different archive name and
different `papa.txt` `D` records**. This is the strongest argument for never letting a scoped
build *upload*.

### 3.8 Global config is writable from any mamefile's `settings()`

`self.config` is public on `BuildTarget` and the README documents mamefiles writing it. Reported
unguarded, root-relevant setters **[R]**:

| Setter | Global effect | Guarded? |
|---|---|---|
| `BuildConfig.set_artifactory_ftp()` | artifactory URL/auth | **No** - bypasses the root guard that exists on the `BuildTarget` wrapper |
| `config.use_gcc_stdlib_for_clang()` | `clang_stdlib` | **No**, and **not part of the build-dir suffix** -> silent libc++/libstdc++ flip into the *same* dir |
| `config.enable_fortran()` | `config.fortran` | **No** |
| `set_arch` / `set_platform` / sanitizer / coverage | `platform_build_dir_name()` -> every dep's `build_dir` | **No** |
| `macos_version` / `ios_version` / `android_api` / `cc_path` | feed `get_distro_info()`/`compiler_version()` -> **archive name** | **No** |
| `prefer_gcc` / `prefer_clang` | compiler | **effectively yes** - `lock_compiler()` runs right after *root's* settings, before any child exists, so sibling calls are already inert **[R]** |

The existing good pattern to copy **[R]** (`build_target.py:232-234`): `if not self.dep.is_root: return`.

Combined with section 2's race, non-root `settings()` today are both **concurrent** and **unguarded**.

### 3.9 What must never be scoped

- **`dirty X`** - `get_deps_that_depend_on_target` **[R]** (`dependency_chain.py:138-166`) is a
  *reverse*-dependency walk over the whole graph, i.e. exactly the sibling information scoping
  discards. Scoped, it under-marks silently -> stale siblings link against a rebuilt X.
- **`clean all`**, bare `list`, bare `deps_only`, `sched_debug`, `target=all` - whole-graph by
  definition.
- **`serial`** - `execute_task_chain` **[R]** (`dependency_chain.py:552-556`) hard-raises
  `"Child target not executed before target which requires it"`. Zero tolerance for a
  non-closed dep list, where the parallel scheduler silently tolerates it.

### 3.10 Things that are already fine (do not "fix")

- `config.loaded_dependencies` is **never iterated** - only a name->instance map inside
  `add_child` **[R]**. Not a whole-graph registry.
- `sweep_orphaned_build_dirs` **[R]** enumerates the workspace **from disk**, not from the graph.
  Scoped loading does not make it delete more. **But** it is gated on `clean_only() &&
  targets_all()` - treat that as an invariant. If a refactor changes how `target=all` is
  represented, this either stops firing (orphans accumulate) or fires on `clean X` and rmtree's
  every package build dir for the platform.
- `mark_unbuilt_target_deps` is already subtree-scoped and already `target is None`-safe.
- **No whole-graph validator exists** (no version-conflict, duplicate-name or url reconciliation
  pass) **[R]** - so scoping loses no validation. It also means section 3.7's silent overwrite is
  undetected today.

---

## 4. Roadmap

### Item 0 - `settings()` under the parallel loader *(external, inbound)*

Owned by another model. Not specified here. **Land first.** Items 2 and 4 touch the same code and
must rebase onto it.

Ask the author to record: does the fix serialize `settings()`, reorder it, or make specific
setters safe? Item 2's scope depends on the answer.

---

### Item 1 - Zombie hardening: guard `dep.target is None` in graph walks

**Standalone value:** yes, independent of scoped discovery. A dep can already have `target is
None` today from an interrupted clone or a mamefile that failed to parse - that is exactly why the
guard at `build_dependency.py:212` was added. Every *other* walk lacks it.

**Reported unguarded dereferences [R]** - re-verify each:
`dependency_chain.py:53-55` (`_get_exported_libs`), `:286-291`
(`_get_dependency_cmake_defines`, `_get_hierarchical_libs`), `:527-529` (`print_dependencies`),
`:690-691` (`_deploy_run_postpass`), `:730-740` (`_reserve_weight`, `_build_detail`),
`:749-765` (`print_sched_debug`), `build_target.py:353` (`_find_target`),
`papa_deploy.py:41,60` (`_gather`), `main.py:210-215` (`print_package_exports`).

**Change sketch:** one predicate (e.g. `dep.is_loaded()` or reuse the existing `target is None`
test) applied consistently. Decide per-site whether to **skip** the dep or **fail loudly**. Prefer
skip in reporting paths, loud in paths that generate build inputs - a silently truncated
`mama-dependencies.cmake` is worse than a crash.

**Risk:** low. Adding guards cannot break a graph where every target is loaded.
**Watch for:** masking a real bug. If a target is None during a *full* load, that is a defect.
Consider a `verbose` warning so it stays visible.

**Tests:** a dep with `target=None` in the tree survives each walk. `_reserve_weight`/
`_build_detail` do not raise. A truncated-but-valid graph still produces correct cmake for the
loaded set. Existing `tests/test_target_scoped_build/...:185` is the precedent to mirror.

**Done:** full suite green. A synthetic zombie in the tree cannot crash any listed walk.
**Rollback:** revert. Guards are additive.

---

### Item 2 - Root-only guards on global setters + `add_child` collision diagnostic

**Standalone value:** yes. Catches real config bugs today, and section 2's race makes non-root
`settings()` mutation actively dangerous *now*.

**Change sketch (two independent halves - consider two commits):**

- **2a.** Apply the existing `if not self.dep.is_root: return` pattern (plus a `warning()`) to the
  setters in section 3.8 that are documented as root-only but unenforced - priority order:
  `use_gcc_stdlib_for_clang`, `BuildConfig.set_artifactory_ftp`, `enable_fortran`,
  `set_arch`/`set_platform`. **Rebase onto Item 0 first**. It may already cover some of these.
- **2b.** In `add_child`, when a name is re-declared with a **different** url/branch/tag/mamefile,
  emit a `warning()` naming both declarations. Do **not** raise - that would break existing
  projects that rely on first-wins.

**Risk:** **medium - this is behavior-changing.** A project today may *depend* on a non-root
mamefile setting one of these (e.g. a dep setting the artifactory URL). Turning that into a no-op
silently changes their build.

**Mitigation:** land 2a as **warn-only first** (log "ignored, root-only" *without* changing
behavior), ship a release, then enforce in a later release. Two-stage. Do not skip this.

**Tests:** non-root setter is ignored + warns. Root setter still applies. Collision warning fires
on differing url and stays silent on identical redeclaration. Existing suites green.

**Done:** warn-only release out, no user reports of intentional non-root use.
**Rollback:** revert. Guards are additive and warn-only in stage 1.

---

### Item 3 - Scope the executed chain for **every** specific-target command

**Prerequisite for Item 4. Also independently reduces wasted work.**

Today **[R]** `main.py:365` computes `flat_deps = get_flat_deps(root)` unconditionally, and only
this predicate (`main.py:371`) narrows it to X's subtree:

```python
targeted = ((config.build or config.upload or config.deploy)
            and config.has_target() and not config.targets_all() and not config.deps_only)
```

So `update X`, `test X`, `start=`, `open`, `wipe`, `unshallow`, bare `coverage-report X` execute
the chain over the **whole graph**. Under a full load that is merely wasteful. Under a *scoped*
load it becomes **catastrophic and silent**: `_configure_body` -> `_save_mama_cmake_and_dependencies_cmake(root)`
-> `_save_dependencies_cmake` **[R]** (`dependency_chain.py:309-336`) rewrites root's
`mama-dependencies.cmake` from a truncated `_get_flattened_deps(root)` and saves it. The next full
root build then silently loses includes and link libs.

**Change sketch:** widen the narrowing to any specific non-`all` target for commands that are
inherently single-target, keeping `dirty`/`list`/`clean all`/bare `deps_only` on the full chain.

**Risk:** **medium-high - user-visible behavior change.** `mama test X` currently walks (and
packages) the whole tree. Narrowing changes what gets built as a side effect. Some users may
depend on that accidentally.

**Tests:** for each command x `target=X`, assert the executed chain equals `get_flat_deps(X)`.
Assert root's `mama-dependencies.cmake` is **not** rewritten by a targeted run. `dirty`/`list`/
`clean all` still see the full chain.

**Done:** full suite green. `test_target_scoped_build`, `test_deps_only`, `test_clean_only`,
`test_main_dispatch` unchanged or consciously updated.
**Rollback:** revert the predicate.

---

### Item 4 - Free-tier scoped discovery *(the actual optimization - do last, or never)*

**Only start this if cold-build timings prove the nested bootstrap is expensive enough to
justify loader risk.** The correctness problem it was originally meant to solve is already fixed
by `3b0c654`.

**Design, corrected by section 3.5:** two tiers only.

- **Free** - mamefile already on disk: `add_local`, `add_git(mamafile=...)` overrides, already-cloned
  trees.
- **Expensive** - clone *or* shim. **Do not build a shim tier** (section 3.5).

Algorithm: BFS from root expanding **only** free children, stopping the moment X is located, then
fully loading X's subtree. **If free expansion is exhausted without finding X, fall back to today's
full `load_dependency_chain`.**

That fallback is what dissolves the chicken-and-egg problem (section 3.9 / `find_dependency` needs
`get_children()`, which is empty for an unloaded dep): on fallback, target resolution and the
`"Available targets: ..."` typo message are byte-identical to today.

**This is sufficient for the motivating case [V]:** the path is
`gcs -> rpclib (add_local) -> protobuf (mamafile="../../mamadeps/protobuf.py")` - every
hop free, zero clones needed for discovery.

**Gating - deliberately narrow:**
- `build target=X` **only**. **Not `upload`/`deploy`** - section 3.7 means a scoped build can pick a
  different declaration winner and therefore publish a package with a different identity and
  different `D` records. Never let that reach a shared artifactory.
- Excluded: `dirty`, `list`, `clean all`, bare `deps_only`, `sched_debug`, `serial`, `target=all`.
- Behind an **opt-in flag** for at least one release.

**Depends on:** Items 1, 2, 3 all landed.

**Tests:** free-tier BFS finds a local/override target without cloning. Unreachable target falls
back to full load and produces the identical error message. A scoped build of X loads exactly the
full-build subtree of X. Ancestors are never revived (fork-bomb invariant). `serial` refuses or is
excluded.

**Done:** opt-in flag green in CI on a real project. Measured saving recorded.
**Rollback:** flag defaults off. Revert is a one-line gate.

---

### Item 5 - Enable scoped discovery for the `build_host_binary` bootstrap child

Trivial once Item 4 is proven: pass the flag in the child argv constructed in
`BuildTarget.build_host_binary`. Keep the `host == host_platform_name()` early-return untouched
(section 3.6).

---

## 5. Explicitly rejected - do not re-propose without new evidence

| Rejected | Why |
|---|---|
| **Shim-expansion tier in discovery** | A shim probe downloads and unzips the entire package to read `papa.txt` (section 3.5). Most expensive option, not cheapest. |
| **`O_CREAT\|O_EXCL` lockfile** | Leaves a stale lock on crash -> hangs a build forever. The landed `flock`/`msvcrt` design is released by the OS on fd close or process death. |
| **Lock file *inside* `dep_dir`** | `reclone_wipe` rmtree's the whole `dep_dir`. Deleting a held lock unlinks its inode and exclusion silently breaks. Sidecar lives in the parent dir. |
| **Bundling a host binary into the Android artifactory package** | The archive name has no host-OS component, so a Linux-published and a Windows-published package collide, and a cross-host consumer gets an unrunnable binary. |
| **In-process cross-platform artifactory fetch (`host_platform=True` flag)** | `artifactory_archive_name` derives every token from the single global config, including an **exact** `compiler` token. Reconstructing a foreign platform's identity in-process means hand-building a second config, and the exact-match key would mostly miss anyway. The subprocess child computes its own correct name. |
| **Suppressing `add_child` for out-of-scope deps** | Would fabricate `'<sibling> was removed'` rebuild triggers via `find_missing_dependency` **[R]**. Zombies must exist. They must be *guarded* (Item 1). |

---

## 6. Invariants - assert these in tests, forever

1. **Never revive ancestors of the target** (section 3.6, fork bomb). Guarded twice: in
   `mark_unbuilt_target_deps` and by `build_host_binary`'s host early-return.
2. `clean_only() && targets_all()` still triggers `sweep_orphaned_build_dirs` (section 3.10).
3. A targeted run never rewrites **root's** `mama-dependencies.cmake` (Item 3).
4. A scoped build of X loads exactly the same dep *set* as a full build restricted to X's subtree.
5. Scoped loading never feeds `upload`/`deploy` (section 3.7).
6. The cross-process dep lock's sidecar stays **outside** the dir it guards.

**Regression suites that must stay green:** `test_target_scoped_build` (15),
`test_deps_only` (17), `test_clean_only` (6), `test_target_args` (4), `test_parallel_load`,
`test_main_dispatch`, `test_dep_dir_lock`, `test_host_binary`, `test_toolchain_change_wipe`,
`test_cmake_cache_repair`, `test_compiler_cache`.

---

## 7. Open questions for the auditing model

1. **Re-verify every [R] claim**, especially section 3.5 (shim cost) and section 3.8 (setter list) - the whole
   plan pivots on those two.
2. Does the Item 0 `settings()` patch make Item 2a redundant, partly or wholly?
3. Item 3 is behavior-changing. Is narrowing `mama test X` to X's subtree *desirable*, or do users
   rely on it building the tree? Needs a product decision, not just a code one.
4. Is Item 4 worth the loader risk at all? Measure first: time a cold
   `mama linux build target=protobuf` and count the downloads/clones it performs. If the saving is
   small, stop after Item 3.
5. Should `add_child`'s silent first-wins overwrite (section 3.7) become an **error** in a future major
   version? It is a genuine latent bug independent of everything here.
6. `execute_unified` already grows lazily (section 3.1). Is extending it with a stop condition a better
   Item 4 than a separate scoped loader, given `_can_unify` currently excludes targeted builds
   precisely because the classic path "resolves the whole tree up front for target lookup"?

---

## 8. Sequencing summary

```
Item 0 (external: settings() parallel fix)     <- land first
   \_ Item 1 (zombie guards)                   <- standalone, low risk, do next
        \_ Item 2 (setter guards, warn-first)  <- standalone, 2-stage release
             \_ Item 3 (scope executed chain)  <- behavior change, needs decision
                  \_ Item 4 (free-tier discovery, opt-in, build-only)   <- optional
                       \_ Item 5 (enable for bootstrap child)
```

Stop at any point. Every prefix of this list leaves the tree stable and better than it started.
