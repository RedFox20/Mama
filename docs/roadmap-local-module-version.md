# An automatic version for a local module

**Status:** planned. Nothing here is built yet.
**Audience:** an engineer or model picking this up cold. This document is self-contained.
**Origin:** KrattGCS asked mama to name a local module by its content. The investigation refuted
three designs, including the two this document started from. Section 2 records the measurements.

**This document records its own corrections.** Three claims in the planning discussion were wrong,
and each one is kept next to the thing it was wrong about. A reader who wonders whether anyone
checked gets an answer instead of repeating the work.

Line numbers drift. Grep for the quoted code, never trust the number.

Read `roadmap-target-version.md` first. It explains why a git dep must name its package before the
clone. This document is about the case where that constraint does not apply.

---

## 0. How to use this document

Sections 1 and 2 are reference: the problem, and the numbers that chose the design. Read section 2
before you propose an alternative, because it already refutes the obvious ones.

Sections 3 and 4 are the plan. **Each phase lands alone and leaves the tree stable.** Do not batch
them. Phase 1 is worth landing even if phase 2 never happens.

Evidence tags:

- **[V]** verified by reading the code and by running it.
- **[M]** measured on this machine. The numbers name the tree they came from.
- **[?]** a design claim about code that does not exist yet. Re-check when you get there.

---

## 1. The problem  [V]

A local module is a directory inside the consumer repository, added with `add_local()`. KrattGCS has
nine of them under `modules/`. Their content changes with every edit, and a package must follow that
content.

Today a local dep cannot name a package at all. `artifactory_archive_name` raises for `p.is_src`
when no version resolved:

```python
elif p.is_src:
    if not version:
        raise RuntimeError(f'Local package {target.name} has no target.version set in mamafile')
```

KrattGCS worked around it with `set_automatic_mama_version()` in its own repository. That helper
walked the module tree, hashed every file with sha256, and assigned the first 16 hex digits to
`self.version`. Mama v0.13.9 and later refuse that upload:

```
Target logging  UPLOAD REFUSED: this build named the package '6507713e48e89f8f',
but a download reads '<commit hash>' from the mamafile.
```

The refusal is correct for a git dep. An upload runs the mamafile, and a download only reads it as
text, because it must name the archive before it clones anything. `scan_mamafile` finds no literal,
so the two sides would disagree.

**The rule is too strict for a local dep.** The source is already on disk. Nothing has to predict a
name before a clone, because there is no clone. So mama can compute the version itself.

### 1.1 What the version must satisfy

1. A local module with no `self.version` gets a computed version, and `mama upload` publishes it.
2. An edit to any file that the build reads changes the version.
3. A build directory inside the module tree never changes the version.
4. Two clean checkouts of the same commit, on two machines, produce the same version.
5. A git dep with a computed version is still refused, exactly as today.

**One earlier requirement is dropped: "an edit to a README must not change the version."** It asked
mama to fingerprint only the files the target exports. That cannot work. The export set comes from
`export_include()` and `papa_deploy()`, which run after configure, and the version is needed before
the build to decide whether to download. The dependency is circular. The requirement is also wrong
on its own terms, because a `.cpp` file is not exported and an edit to one must change the version.
A README edit costs one wasted rebuild. A stale package that answers for new sources costs
correctness. Take the wasted rebuild.

---

## 2. The measurements that chose the design  [M]

Measured on KrattGCS at commit `a68167967`: 9 modules, 170 files, 528 KB after the ignore lists.
Median of 7 runs, warm file cache, Windows 11.

### 2.1 A git subprocess is the slowest option, and the repo size is not why

The first design used `git rev-parse HEAD:<rel_path>`, the subtree object hash. It is
content-addressed, so two checkouts agree by construction. It lost on cost.

| method | median |
|---|---|
| `git rev-parse`, one process per module | 256.6 ms |
| `git rev-parse`, ONE batched process | 31.2 ms |
| `git rev-parse` plus a `git status` dirty guard | 66.7 ms |
| sha256 over file content, the KrattGCS helper | 12.7 ms |
| crc32 over path, mtime and size | 3.4 ms |

**Correction: the repo size is not the cause.** KrattGCS holds 20448 commits, 2729 tracked files and
a 566 MB pack. A 1-commit empty repo answers no faster:

| | median |
|---|---|
| `git --version`, which touches no repo | 22.7 ms |
| `git rev-parse HEAD` in KrattGCS, 566 MB pack | 25.4 ms |
| `git rev-parse HEAD` in Mama, 4 MB pack | 26.3 ms |
| `git rev-parse HEAD` in a 1-commit empty repo | 24.5 ms |

Windows process creation costs about 23 ms. Git's own work costs 2 ms to 3 ms on top. **Every git
call in mama pays that 23 ms, whatever it asks for.** Plan around the call count, never around the
repo size.

### 2.2 The hash algorithm does not matter, because file IO dominates

| step | median | delta |
|---|---|---|
| `os.walk` only | 1.69 ms | |
| walk plus `os.stat` per file | 2.62 ms | +0.93 |
| walk plus read every byte, NO hash | 9.85 ms | +7.23 |
| ...plus crc32 | 10.12 ms | **+0.27** |
| ...plus sha1 | 10.58 ms | **+0.73** |
| ...plus sha256 | 10.38 ms | **+0.53** |

Reading 528 KB across 170 files costs 7.2 ms. Hashing it costs under 1 ms. The three hash rows sit
inside the measurement noise of each other, and sha256 measured faster than sha1.

**So crc32 buys about 0.4 ms and gives up collision safety.** A 32-bit value collides by birthday at
about 65000 distinct versions, and a version collision makes a consumer download the wrong binary
with no error. Truncated sha1 at 64 bits moves that to about 4 billion. Mama uses sha1.

**The win comes from the memo, not from the hash.** The stat-only row is 2.62 ms. A run whose files
are unchanged never opens one, so it lands there: 10 ms to 2.6 ms, a 74 percent cut.

### 2.3 CRLF normalization costs 1 percent

Git may store LF and check out CRLF. A machine with `core.autocrlf=true` then holds different bytes
for identical sources, and a content hash names them differently.

| tree | read plus sha1 | plus NUL test | plus CRLF fold |
|---|---|---|---|
| 170 files, 528 KB | 8.22 ms | 8.06 ms | 8.29 ms |
| 2041 files, 34 MB | 115.7 ms | 116.3 ms | 123.0 ms |

The binary test is free and the fold costs 1 percent on the real tree. Mama always normalizes.

**Correction: an earlier draft called this a proven portability defect.** The evidence was 9 files
whose bytes differed from the recorded blob. Those 9 are the `modules/*/mamafile.py` files that
KrattGCS had edited, and a content hash should notice them. The other 152 files matched exactly,
because this repo uses `core.autocrlf=input`, which does not convert on checkout. The risk is real
for `autocrlf=true` and it was not demonstrated here.

### 2.4 What mama already pays, per build

| approach | median | spawns |
|---|---|---|
| per-dep `git status`, scoped, what mama does today | 297.0 ms | 9 |
| ONE `git status`, all module paths as arguments | 34.6 ms | 1 |
| ONE `git status`, whole repo, no path arguments | 119.7 ms | 1 |

`git_dir_fingerprint` runs one `git status` per local dep. Nine local deps cost 297 ms on every
build, before any of this feature exists. That is larger than the feature it would pay for.

The 34.6 ms row needs the full list of local dep paths, and mama never holds one: a nested module
declares its own `add_local` children, which stay unknown until that module loads. The 119.7 ms row
needs no path list at all. Phase 1 takes that row.

---

## 3. Phase 1 - one status for the whole run  [?]

**This phase stands alone. Land it even if phase 2 never happens.** It removes 177 ms from every
build of a project with local deps, and it is a prerequisite for the phase 2 dirty guard.

### 3.1 What changes

Run `git status --porcelain -z` once for the root repository, before the dependency walk starts,
and hand the parsed result down. A local dep filters its own entries by a path prefix.

Run it **first and eagerly**, not lazily on first use. Three reasons:

1. Parallel load means several local deps can ask at the same moment. An eager call needs no lock
   and cannot race.
2. The root target learns its own status from the same call.
3. One call site is easier to reason about than a memoized accessor with a lock.

### 3.2 Where it goes

| what | where |
|---|---|
| the one status call and its parse | new, `mama/util.py` or a small `mama/types/repo_status.py` |
| the per-dep filter | `LocalSource.working_tree_fingerprint` |
| today's per-dep status call | `util.git_dir_fingerprint`, which stops calling status itself |

`git_dir_fingerprint` keeps its `git diff HEAD` and `git ls-files --others` calls. Those fire only
for a dep that status already reported as dirty, which is rare. The recent change that gates them on
status makes this refactor smaller, because the status result is already the decision point there.

**One defect to fix while you are in there [V]:** the status call passes `-- .` and the
`git ls-files --others` call passes no pathspec. Both are scoped by `cwd=src_dir`, so the behavior is
right, and the asymmetry reads as a bug. Give both an explicit pathspec.

### 3.3 What must not break

- A local dep that is not under git. The status call fails, and every dep reads an empty result.
- A local dep in a DIFFERENT repository from the root. Key the cached status by repository root, and
  run a second status for a second root.
- `mama build` with no local deps. Do not pay for a status nobody reads.

---

## 4. Phase 2 - the computed version  [?]

### 4.1 The value

For a `LocalSource` whose mamafile pins no `self.version`:

```python
h = hashlib.sha1()
for rel_path, file_hash in sorted(entries):     # sorted, so the walk order never changes the value
    h.update(rel_path.encode()); h.update(b'\0'); h.update(file_hash)
version = h.hexdigest()[:16]
```

`file_hash` is `sha1(normalized_bytes).digest()`, 20 bytes. Normalized means CRLF folded to LF when
the first 8000 bytes hold no NUL byte, which is git's own text rule.

The version field reads `local-<10 hex digits>`, so the archive name ends with it:

```
logging-ubuntu-24-gcc14.3-x64-release-local-4b52af2211
```

The `local-` prefix tells a reader of an artifactory listing which packages a source tree named,
against which a commit or a tag named. 10 hex digits is 40 bits. The fields before the version
already separate module, platform, os major, compiler, arch and build type, so one namespace holds
the published builds of ONE module for ONE config. At 1000 of those the odds of a collision are 1 in
2.2 million, and at 10000 they are 1 in 22 thousand. Widening the field costs nothing but a
rebuild wave, so raise `_DIGEST_CHARS` if a project ever publishes at that scale.

**The NUL byte between the path and the hash is load-bearing.** Without a delimiter the pairs
`("ab", "c")` and `("a", "bc")` produce one value. A path cannot hold a NUL byte, and the digest has
a fixed width, so one NUL separates them unambiguously.

**An earlier draft wrapped each file in git's blob envelope, `b'blob %d\0' + data`.** The only gain
was a value equal to git's own blob hash, which mattered only while the design still called git. It
also copies the whole buffer to concatenate, which cost 6 ms on a 34 MB tree. Dropped.

### 4.2 The memo

A file whose size and mtime are unchanged reuses its stored hash, so a warm run opens no file.

- Store `path -> (size, mtime_ns, sha1)` in the dep's build dir, next to `src_status`.
- `mama update` invalidates the whole memo. A fetch can produce a new file with an old mtime.
- A memo miss reads the file and rewrites the entry.
- A corrupt or unreadable memo is a miss, never an error.

Measured target: 10 ms cold, 2.6 ms warm, for the KrattGCS tree.

### 4.3 The ignore list

Static, and derived from config where mama already names the value:

| entry | source |
|---|---|
| the workspace dir, default `packages` | `BuildTarget.workspace` |
| the build dir, for example `linux-clang-asan` | `build_names.build_dir_name(config, variant)` |
| `.git`, `.mama`, `__pycache__` | literal |
| `.o .obj .a .so .dll .dylib .lib .pyc` | literal |

The workspace dir matters because `workspaces_root` becomes the root source dir for a project-local
workspace [V]:

```python
if not self.config.global_workspace:
    self.config.workspaces_root = self.src_dir
```

So build output lands in `<root>/packages/<name>/<platform>/`, inside the working tree. An earlier
draft claimed it lands in `$HOME` and that the ignore list barely mattered. That claim read the
pre-parse default at `build_config.py` and missed the reset above. It was wrong.

**Do not reuse `_NON_LIB_DIRS` from `build_target.py` [V].** It serves the source-file TU fallback
and excludes `test`, `docs`, `external` and `third_party`. All of those can change what a build
produces, so a version that ignored them would miss a real source edit.

### 4.4 The dirty guard, and the upload

Uncommitted edits have no name that another machine can reproduce. That is requirement 4.

- A clean module subtree gets the computed version, and the upload proceeds.
- A dirty module subtree gets the same version plus a marker, and the upload **stops**.
- The stop is graceful. Call `warning()`, skip the upload, and let the build finish. It is not an
  exception, the same way a 404 for a git dep is not.

The dirty answer comes from the phase 1 status. It costs no extra process.

### 4.5 Where the two readers meet

`papa_upload._download_can_find_this_version` compares the executed `target.version` against
`pinned_version(target.dep)`, which scans the mamafile text. For a `LocalSource` that comparison has
no purpose, because both sides read the same tree on the same disk. Route a local dep to the computed
value on both sides, so they agree by construction.

This also unblocks a consumer who wants to compute the version their own way, from a `VERSION` file
or from `git describe`. For a local dep, the executed value wins.

---

## 5. Acceptance

1. A local module with no `self.version` gets a computed version, and `mama upload` publishes it.
2. An edit to any file the walk visits changes the version.
3. A build dir or a `packages` dir inside the module tree never changes the version.
4. Two clean checkouts of the same commit, on two machines, produce the same version.
5. A git dep with a computed version is still refused, exactly as today.
6. A dirty module subtree refuses the upload through `warning()`, and the build still finishes.
7. A project with nine local deps runs ONE `git status`, not nine.

---

## 6. What NOT to do

- **Do not call git per dependency.** Windows charges 23 ms per process before git does any work.
  Count the calls, not the bytes.
- **Do not use crc32 for a published name.** It saves 0.4 ms and buys a collision that silently
  serves the wrong binary.
- **Do not fingerprint mtime for a published name.** A fresh checkout stamps every file with the
  checkout time, so no two machines agree. Mtime belongs in the memo, where it only decides whether
  to re-read a file.
- **Do not scope the version to the export set.** The export set is unknown until after configure,
  and the version is needed before the build.
- **Do not publish a package for a dirty tree.** No other machine can reproduce the name.
- **Do not relax the text-scan rule for a git dep.** Section 2 of `roadmap-target-version.md` says
  why, and nothing here changes it.
