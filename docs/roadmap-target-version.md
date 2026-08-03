# Roadmap: a robust `target.version`, and a `def version(self)` that may compute one

**Status:** P1 (§3) and P2 (§4) have landed. P3 and P4 are still planning only.
**Audience:** an engineer or model picking this up cold. This document is self-contained.
**Origin:** a mamafile moved `self.version` into `settings()` and asked whether the artifactory fetch
still sees it. It does. The investigation found a different and worse problem, in §2.

Line numbers were accurate at commit `2ad43c1`. Grep for the quoted code, never trust the number.

---

## 0. How to use this document

Four phases, ordered by value per risk. **Each phase lands alone and leaves the tree stable.** P1
carries most of the robustness and touches no feature surface, so land it first even if the rest never
happens. P2 names a tag-pinned package after its tag. P3 is the general escape hatch. P4 is cleanup.

Evidence tags:

- **[V]** verified by reading the code and by running it during this analysis.
- **[?]** a design claim about code that does not exist yet. Re-check when you get there.

---

## 1. How the version works today  [V]

`self.version` replaces the commit hash in the last field of the archive name
(`artifactory.py:artifactory_archive_name`):

```
libffmpeg-ubuntu-24-gcc14.3-x64-release-8.0.1        # pinned
libffmpeg-ubuntu-24-gcc14.3-x64-release-df76b66      # unpinned: the commit hash
```

**The download side never runs the mamafile.** It cannot: to skip a clone it needs the archive name
before any source exists. So it reads the mamafile as TEXT and regexes the value out:

```python
# mama/types/git.py
_SELF_VERSION_RE = re.compile(r"""^\s*self\.version\s*=\s*['"]([^'"]+)['"]""", re.MULTILINE)
```

Two entry points, both parse-only:

| when | function | where the text comes from |
|---|---|---|
| pre-clone | `Git.fetch_self_version_from_remote` | a shallow blob-less clone into a tempdir, then `git show HEAD:mamafile.py` |
| post-clone | `artifactory.resolve_pinned_version` | the mamafile on disk |

The post-clone call sits in `artifactory_fetch_and_reconfigure`:

```python
if not target.version:
    target.version = resolve_pinned_version(target.dep)
```

**The upload side is the opposite.** `papa_upload` names the archive from the executed `target.version`,
long after `settings()` and `configure()` have run.

Consequences worth internalizing:

1. The METHOD does not matter. `init()`, `settings()` and `configure()` all work, because the regex is
   method-agnostic. Verified by running `extract_self_version` on a mamafile that assigns inside
   `settings()`. The premise that started this investigation ("settings runs after the fetch, so the
   version is missing") is false.
2. `dep.mamafile` (a parent-repo override) disables the remote probe entirely: `git show HEAD:<path>`
   cannot resolve a path that belongs to the parent repo. `fetch_self_version_from_remote` returns None
   on purpose.
3. The shim probe only runs when there is no working tree. An already-cloned dep only ever uses the
   post-clone path.

---

## 2. The actual defect: parsed name vs executed name  [V]

The two sides can disagree, and when they do, nothing says so.

**2a. First literal wins, silently.** The regex `search`es and returns match #1 in file order:

```python
def settings(self):
    self.version = "8.0.1"
    if 'LGPL' in self.args: self.version = "8.0.1-lgpl"
```
```
extract_self_version(text) -> '8.0.1'          # always, whatever the args are
```

An LGPL build therefore **uploads** `...-8.0.1-lgpl` and every consumer **downloads** `...-8.0.1`.
Result: a permanent cache miss. Every build re-clones and rebuilds, no warning, no error, and the
uploaded artifact is never used by anyone.

**2b. A computed value is invisible.** `self.version = f'{v}'`, a call, or a file read returns `None`,
so the download falls back to the commit-hash name while the upload publishes the computed one. Same
outcome as 2a.

**2c. Nothing verifies the two agree.** There is no check anywhere that the name mama uploads is the
name mama would look for. The divergence is only observable as "the cache never hits", which reads as a
network or artifactory problem.

This is why `add_git(..., args=[...])` exists (landed in `a34f3c4`): args are known before the clone, so
a per-consumer variant needs no version branching. §4 keeps that as the recommended answer, and stops
treating the version as a place to encode variants.

---

## 3. P1 - make the current contract enforceable and loud  [IMPLEMENTED]

**Goal:** no silent divergence, ever. No new mamafile surface. This is the phase that actually removes
the bug class, so it lands first.

Three notes on what shipped, where it differs from the sketch below:

1. The scan reads line by line, not with one anchored regex. The sketch missed the shape that matters
   most: `if lgpl: self.version = '8.0.1-lgpl'` is not at the start of a line, so a `^`-anchored regex
   never saw the second assignment and reported one trustworthy literal.
2. The trust rule lives in `Git.trusted_self_version`, so both readers share it. `extract_self_version`
   stays a pure text function and warns about nothing.
3. The warning names the fix that exists today (one raw string literal). It does not point at P2 or P3,
   because neither is built yet. Update the message when they are.

Tests live in `tests/test_self_version_probe/` (the scan, next to the probe tests that already own it)
and `tests/test_target_version/` (the trust rule, the warning and the upload guard).

### 3.1 `extract_self_version` reports what it saw

Return a small result instead of a bare string:

```python
class VersionScan(NamedTuple):
    value: str        # the single literal, or ''
    literals: int     # how many `self.version = '<literal>'` lines the text holds
    computed: bool    # a `self.version =` line whose value is not a quoted literal
```

- `literals == 1 and not computed` -> the only trustworthy case. Use `value`.
- `literals > 1` -> **ambiguous**. Do not guess. Warn once naming the mamafile, and return '' so the
  dep names itself by commit hash on both sides (consistent, if less useful).
- `computed` -> **unprobeable**. Warn once, return ''. Point the message at P2/P3.

`warning()` from `mama.utils.system`, once per dep per run, and only when `config.print`.

### 3.2 The upload refuses to publish a name the download cannot find

In `papa_upload`, before the FTP put, recompute the version through the download path.
`resolve_pinned_version` reads the dep's own mamafile. Compare that with the executed `target.version`.

- equal, or both empty -> proceed.
- different -> `error()` naming both strings, and **skip the upload** rather than publish an
  unreachable artifact. This is the check that would have caught 2a on the first run.

This needs the dep's mamafile on disk, which is always true at upload time [V].

### 3.3 Tests

`tests/test_target_version/`, new:

1. one literal in `init()` / `settings()` / `configure()` -> found, identical result (pins §1.1)
2. two literals -> `literals == 2`, value '' , one warning
3. computed assignment (f-string, call, name) -> `computed`, value '', one warning
4. no assignment -> clean empty scan, no warning
5. upload with a matching version -> proceeds
6. upload with a diverging version -> refused, message names both names
7. an existing pinned dep still resolves the same archive name (regression guard on `test_papa_upload`)

**Risk:** a mamafile that holds two literals today and relies on the first one. After this change it
gets the hash name, which costs a one-time cache miss and prints a warning. That beats the current
silent mismatch, but it IS a behavior change. Name it in the release notes.

**Effort:** half a day including the review loop.

---

## 4. P2 - a git pin names the package by default  [IMPLEMENTED]

**Goal:** a dep pinned to a tag or a branch names its package after that pin, with no mamafile change
and no code execution. The pin is the version a human already wrote.

```python
self.add_git('qcoro', 'https://github.com/qcoro/qcoro.git', mamafile='mamadeps/qcoro.py', git_tag='v0.13.0')
# -> qcoro-ubuntu-24-gcc14.3-x64-release-v0.13.0        (was: ...-release-a1b2c3d)
```

### 4.1 Why the tag, and why by default

The pin comes from the consumer, so mama holds it before it clones anything, exactly like the platform
and the compiler [V]. Both the download and the upload read the same `dep_source.tag`, so the two sides
cannot diverge the way §2 describes. No text scan, no execution, no trust decision.

It also removes work: naming a tag-pinned package no longer needs the commit hash, so the probe skips
its `git ls-remote` for that dep.

Default rather than opt-in, because the alternative is worse. A tag-pinned dep whose archive carries a
commit hash re-uploads under a new name every time upstream re-tags that release. No consumer reading
the tag can find it.

### 4.2 Precedence, highest first

| pin | version field | why |
|---|---|---|
| `self.version = '<literal>'` in the dep's mamafile | `8.0.1` | the dep states its own version, and P1 made that statement trustworthy |
| `git_tag='v0.13.0'` | `v0.13.0` | a tag is immutable by convention, so it identifies the source on its own |
| `git_branch='feat/experimental-radio'` | `feat-experimental-radio-a1b2c3d` | a branch MOVES, so it labels the name and the commit still identifies the source |
| `git_commit='4acd905...'` | `a1b2c3d` | `add_git` stores a commit pin in the tag field, and `Git.is_hex_string` tells the two apart the way the clone path does |
| nothing | `a1b2c3d` | nothing named this build, so identity is the only name left |

A root target, an `add_artifactory_pkg` and a local source keep their current naming untouched.

### 4.2.1 Why a branch cannot replace the hash

Naming a branch build `pkg-...-feat-experimental-radio` alone would be wrong in the one way that never
reports itself. The branch moves, so that one name would cover every commit ever pushed to it. A
consumer that resolved commit X would download whichever build landed last, silently, which is §2 in a
new costume.

Keeping the hash means a branch build still caches correctly: same commit, same name, cache hit. The
branch name only makes the archive readable to a human scanning the server.

### 4.3 The name a tag produces

`build_names.sanitize_version` keeps `[A-Za-z0-9._-]` and collapses every other run into one `-`. The
tag `release/1.0` becomes `release-1.0`, and the name stays valid on FTP and on a Windows disk.

It parses nothing else. Real pins look like `1.0.0`, `v0.1.3`, `n8.1.0`, `RELEASE_2_1` and
`feat/experimental-radio`, and any rule that assumes a shape breaks on the next repo. A pin that is
entirely unsafe characters sanitizes to '', and the dep falls back to the next source in the table.

Two rules that look wrong until you try the alternative:

- **Keep the leading `v`.** An earlier draft of this document stripped it. Do not: a repo carrying both
  `1.0` and `v1.0` would then publish two different sources under one name.
- **Keep the case.** Lowercasing merges `v1.0` and `V1.0` into one name for two tags. The existing
  literal path does not change case either.

### 4.4 What this does NOT do

**No `version_from` mode, and no way to force the commit hash on a tag-pinned dep.** An earlier draft
proposed `version_from = 'tag' | 'branch' | 'commit'`. The tag default covers the stated need without it,
and the mode carries a cost the tag does not. `DepSource.papa_join` is positional, so a package's
papa.txt would need a sixth field, and every older papa.txt would then misparse its args. The tag itself
already round-trips [V], so the default survives a papa.txt hop for free.

Pin a commit instead of a tag when the build must carry its identity. If a real case for the mode
appears, add it as an `add_git(version_from=...)` argument on the CONSUMER side. Never add it as a
mamafile attribute. No reader can see a mamafile attribute before the clone, which is the §2 defect.

### 4.5 Migration

Every pinned dep gets a new archive name once, so the first build after this misses the cache and
republishes. That is every tag-pinned dep AND every branch-pinned one, so expect the wave to cover most
of a normal dependency tree.

Two consumers that pin the same tag now share one archive, where before each resolved its own hash.
One consumer may pin a commit while another pins the tag of that same commit. They then upload two
archives of one source, which wastes space but breaks nothing.

**Risk:** a moved tag. Upstream re-pointing `v0.13.0` at a new commit makes the new build upload over
the old archive. `check_status` still compares the recorded commit, so a local tree rebuilds correctly.
This risk is the one `self.version = '8.0.1'` has always carried, now on more deps.

**Effort:** implemented in about half a day.

---

## 5. P3 - `def version(self)`, executed in a probe context

**Goal:** the general escape hatch, for a version that genuinely needs code (parse a `CMakeLists.txt`,
read a `VERSION` file, combine a tag with a build flag).

### 5.1 The Python mechanic that decides the shape  [V]

`BuildTarget.__init__` does `self.version = ''`. An instance attribute **shadows** a subclass method of
the same name, so a mamafile's `def version(self)` would never run while that assignment stays
unconditional. Two ways out:

- **(a) Keep the name `version`.** `__init__` assigns the attribute only when the subclass has not
  defined a callable: `if not callable(getattr(type(self), 'version', None)): self.version = ''`.
  Then every reader goes through one accessor:
  ```python
  def target_version(target) -> str:
      v = getattr(type(target), 'version', None)
      return str((target.version() if callable(v) else getattr(target, 'version', '')) or '')
  ```
  Ergonomics: exactly the shape this roadmap was asked for. Cost: one conditional in `__init__`, plus
  one accessor that every consumer must use. A mamafile that defines the method and ALSO assigns `self.version = x`
  somewhere silently re-shadows it -> P1's scan must flag that combination.
- **(b) A separate hook name**, e.g. `def compute_version(self)`, leaving `version` a plain attribute.
  Simpler Python, no shadowing trap, but two names for one concept, which the codebase avoids.

Recommendation: **(a)**, with the mixed-usage warning from P1.

### 5.2 Running it before a clone  [?]

`fetch_self_version_from_remote` already fetches the mamafile text into a tempdir, and
`parse_mamafile` already imports a mamafile dynamically and instantiates its target class [V]. So the
pre-clone probe is: fetch the text -> import it in the tempdir -> construct a probe target -> call
`version()`.

The probe target gets only what is knowable pre-clone: `config`, `dep`, `args`, and the dep's pinned
branch/tag/commit. Mama configures nothing else.

**This executes code from the remote repo before mama has cloned it.** The trust boundary does not
change. Mama clones and executes that same file seconds later, and the consumer's own mamafile named
the url. Only the timing changes. Defining `def version(self)` is the opt-in.

### 5.3 The contract, which the hook's docs must carry

`version()` MUST be:

1. **Deterministic.** Same dep pin + same args -> same string, on any machine, with no working tree.
2. **Limited to pre-clone inputs.** `self.args`, `self.config`, the dep's pin, and the mamafile's own
   constants. Not the working tree, not the network, not env vars, not the clock.
3. **Side-effect free.** It runs in a probe context. Mama throws away anything it mutates.
4. **Filename-safe and non-empty.** mama normalizes the return through the shared token rules and
   raises a clear error on an empty or non-string return.

A `version()` that reads the working tree is the one shape that cannot work, because the pre-clone probe
has no tree. It must fail loudly with that exact explanation rather than silently returning ''.

### 5.4 Caching

Re-probing per run costs a blob-less fetch per unpinned dep. Persist the resolved version where the
resolved archive name already lives:

- `dep.write_shim_marker(archive_name, commit_hash)` already records the archive [V], and
  `git_status` already records url/branch/tag/commit, which `check_status` compares [V].
- Add the resolved version to that record and reuse it while url, branch, tag, commit and args are
  unchanged. Any change to those already forces a re-resolve, which is exactly the right invalidation.

**Tests:** a method that returns a literal. A method that derives from the tag. A method that reads the
working tree, which must fail with the pre-clone explanation. A non-string return. An empty return. A
probe result reused across two runs. A method plus a stray `self.version =` assignment, which warns.
Pre-clone and post-clone that agree.

**Effort:** two to three days. Most of it is the probe context and its tests, not the accessor.

---

## 6. P4 - retire the text scan for everything except the literal fast path

Once P2 and P3 exist, the regex is only an optimization: it answers "one literal, no fetch needed"
without touching the network. Keep it for that, and route every other shape through P2/P3. Then delete:

- the `computed` fallback path (P3 replaces it),
- the first-literal-wins behavior (P1 already refuses it),
- the special case in `fetch_self_version_from_remote` for `dep.mamafile`, IF the P3 probe can import a
  parent-override mamafile from the parent's own tree, which is on disk pre-clone [?].

**Effort:** half a day, mostly deletions and doc updates.

---

## 7. What NOT to do

- **Do not encode a per-consumer variant in the version.** That is what `add_git(args=[...])` is for.
  Args are known pre-clone by construction, and they already name both the archive and the build dir.
- **Do not make the version depend on the platform, the arch, the compiler, coverage or sanitizers.**
  Those are separate fields in the archive name already. A version that varies by them produces two
  names for one axis, which is the same defect as §2.
- **Do not resolve the version by cloning.** The whole point of the pre-clone probe is that a cache hit
  must not cost a clone. A design that clones to learn the name has no reason to exist.
- **Do not let `version()` read the built tree.** It runs before configure, before build, and
  sometimes before any source exists.

---

## 8. Done criteria

- P1: two mamafiles that would previously diverge now either agree or warn, and no upload can publish a
  name the download path cannot construct. Full suite green.
- P2: a dep can name its archive from its git tag or branch with no code and no extra fetch, and the
  pre-clone and post-clone answers are identical.
- P3: `def version(self)` runs pre-clone, mama caches its result in the dep's status, and every
  violation of §5.3 fails with a message that names the rule it broke.
- P4: one code path per version shape, and the README's rules in "Pinning a version" shrink to
  "a literal, `version_from`, or `def version()`".
