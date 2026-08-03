# The version field: how it works now, and what is left to build

**Status:** P1 and P2 have landed (§3). §4 corrects a claim an earlier draft got wrong. §5 and §6 are
still planning.
**Audience:** an engineer or model picking this up cold. This document is self-contained.
**Origin:** a mamafile moved `self.version` into `settings()` and asked whether the artifactory fetch
still sees it. It does. The investigation found a worse problem, in §2, and P1 and P2 fixed it.

Line numbers drift. Grep for the quoted code, never trust the number.

---

## 0. How to use this document

§1 and §2 are reference: what the version field is, and why it has the shape it has. Read them before
you change anything, because most of the rules exist to stop one specific failure.

§4 onward is planning. **Each item lands alone and leaves the tree stable.** Do not batch them.

Evidence tags:

- **[V]** verified by reading the code and by running it.
- **[?]** a design claim about code that does not exist yet. Re-check when you get there.

---

## 1. The version model as it stands  [V]

The last field of an artifactory archive name identifies the SOURCE the package was built from:

```
{name}-{platform}-{os_major}-{compiler}-{arch}-{build_type}[-variant]-{version}
qcoro-ubuntu-24-gcc14.3-x64-release-v0.13.0
```

`artifactory_archive_name` composes it, and it is the only place that does [V]. It takes the first
version the dep has:

| the dep has | version field | why |
|---|---|---|
| `self.version = '8.0.1'` in its mamafile | `8.0.1` | the dep states its own version, and P1 made that statement trustworthy |
| `add_git(..., git_tag='v0.13.0')` | `v0.13.0` | a tag is immutable by convention, so it identifies the source alone |
| `add_git(..., git_branch='feat/radio')` | `feat-radio-a1b2c3d` | a branch MOVES, so it labels the name and the commit still identifies the source |
| `add_git(..., git_commit='4acd905...')` | `4acd905` | `add_git` stores a commit pin in the tag field, and `Git.is_hex_string` tells the two apart, the way the clone path does |
| nothing | `a1b2c3d` | nothing named this build, so identity is the only name left |

Two readers must agree on that value, and they work differently:

| side | how it reads the version | when |
|---|---|---|
| download | reads the mamafile as TEXT, never runs it | before any clone exists |
| upload | runs the mamafile and uses the value in memory | after `settings()` and `configure()` |

The download side cannot run anything, because naming a package before the clone is the entire point of
the artifactory shim. Every rule below follows from that one constraint.

### 1.1 The rules, and the constraint each one serves

1. **`self.version` must be ONE raw string literal.** The text reader takes it verbatim. It cannot
   evaluate an f-string, a call or a name.
2. **The method does not matter.** `init()`, `settings()` and `configure()` all work, because the reader
   scans text, not scope [V].
3. **A second assignment refuses the value.** The reader cannot know which branch runs. Mama warns, and
   both sides fall back to the commit hash rather than pick one.
4. **A pin needs no reader at all.** A tag, a branch and a commit come from the consumer's `add_git`.
   Both sides hold the same string, with no text scan and no network call.
5. **A pin stays verbatim, minus characters a file name cannot hold.** `release/1.0` becomes
   `release-1.0`. No stripped `v`, no lowercasing: `v1.0`, `V1.0` and `1.0` may be three tags in one
   repo, and two sources must never share one archive name.

### 1.2 Where the code lives  [V]

| what | where |
|---|---|
| the one name composer | `artifactory.artifactory_archive_name` |
| the text scan | `Git.extract_self_version` -> `VersionScan(value, literals, computed)` |
| the trust rule and its warning | `Git.trusted_self_version` |
| the on-disk reader | `artifactory.resolve_pinned_version` |
| the pre-clone reader | `Git.fetch_self_version_from_remote` |
| pin sanitizing | `build_names.sanitize_version` |
| the upload guard | `papa_upload._download_can_find_this_version` |

---

## 2. Why it looks like this: the defect P1 removed  [V]

Before P1 the two sides could disagree, and nothing said so.

```python
def settings(self):
    self.version = "8.0.1"
    if 'LGPL' in self.args: self.version = "8.0.1-lgpl"
```

The old reader took the first literal, always. An LGPL build **uploaded** `...-8.0.1-lgpl` while every
consumer **downloaded** `...-8.0.1`. The result was a permanent cache miss. Every build re-cloned and
rebuilt, with no warning, and nobody ever used the uploaded artifact. A computed version failed the same
way, because the reader returned nothing and the download fell back to the commit hash.

Keep this in mind when you extend the model. **Any version input that one side can see and the other
cannot recreates this defect.** That single test rejects most tempting designs, including every one in
§7.

---

## 3. What landed

### 3.1 P1 - the scan reports, and the upload refuses  [IMPLEMENTED]

- `Git.extract_self_version` returns `VersionScan(value, literals, computed)` instead of a bare string.
  One literal and no computed assignment is the only trustworthy shape.
- `Git.trusted_self_version` applies that rule for both readers, and warns once per dep on a shape it
  refuses.
- `papa_upload` recomputes the version through the download path. When the two disagree it **skips the
  upload**, rather than publish an archive no consumer can ask for.

The scan reads line by line. An anchored regex misses `if lgpl: self.version = '8.0.1-lgpl'`, which is
the shape that matters most.

Tests: `tests/test_self_version_probe/` (the scan) and `tests/test_target_version/` (the trust rule, the
warning, the upload guard).

### 3.2 P2 - a git pin names the package  [IMPLEMENTED]

The precedence table in §1. A tag names the package alone, a branch labels the commit, and a commit pin
shortens to the hash. `build_names.sanitize_version` makes any of them a legal file name.

Naming by tag also removes work: a tag-pinned package needs no `git ls-remote` to name itself.

Tests: `tests/test_version_from_pin/`.

**Migration:** every tag-pinned AND branch-pinned dep got a new archive name once, so the first build
after this republishes most of a normal dependency tree. Two consumers pinning one tag now share an
archive, where before each resolved its own hash.

**Risk:** a moved tag. Upstream re-pointing `v0.13.0` at a new commit makes the next build upload over
the old archive. `check_status` still compares the recorded commit, so a local tree rebuilds correctly.
This is the risk `self.version = '8.0.1'` always carried, now on more deps.

---

## 4. Correction: a consumer-side override IS implementable  [V]

An earlier draft claimed a consumer-side `version_from` could not work, because `DepSource.papa_join` is
positional and a sixth field would make every older papa.txt misparse its args. **That reasoning was
wrong.** It assumed a positional field.

### 4.1 The constraint that actually matters

A published package's papa.txt records its own child deps, so a consumer that downloads the package can
rebuild them. **Many packages are already cached with today's papa.txt, and none of them may need a
rewrite.** Any design that requires re-publishing the cache is rejected on that ground alone.

### 4.2 What the current parser already accepts  [V]

`Git.from_papa_string` reads five positional fields and treats the rest as args. A KEYED token in that
tail costs nothing:

```
qcoro,https://x/qcoro.git,,v0.13.0,mamadeps/qcoro.py,version_from=commit,LGPL
```

Measured against the CURRENT parser [V]:

| case | result |
|---|---|
| new mama reads an OLD papa.txt | identical to today. No `version_from=` token is present, so nothing changes |
| new mama reads a new papa.txt | reads the token, and keeps the remaining tokens as args |
| **old** mama reads a new papa.txt | the token leaks into `args`, so that child names itself `...-versionfromcommit-...` and misses its cache |

**No cached package needs a rewrite**, because mama only writes the token for a dep whose owner asked
for the non-default. Every existing papa.txt keeps working unchanged, on every mama version.

Only the third row costs anything, and only for a dep that opted out. That cost is a cache miss, not a
wrong download: the older mama looks for a name nobody published, so it builds from source.

### 4.3 The cheaper option: do not touch papa.txt at all

An override declared in a mamafile you control never needs to round-trip, because your mamafile is the
thing being read. papa.txt only matters for a TRANSITIVE dep, which a consumer rebuilds from a
downloaded package's records.

So the feature can ship in two steps:

1. **Direct deps only.** `add_git(..., version=...)` in your own mamafile. Zero papa.txt change, zero
   risk to the cache. A transitive dep rebuilt from papa.txt falls back to the default naming, so it
   misses its cache and builds from source. Nothing breaks.
2. **Transitive deps**, later and only if step 1 proves the gap hurts. Add the keyed token from §4.2,
   written only for a non-default dep.

Ship step 1 first. It answers the stated need, and it cannot disturb a single cached package.

---

## 5. What is actually left, ranked

P1 and P2 cover the cases that turned up in practice. Three gaps remain. Each states what a user cannot
express today.

### 5.1 A consumer cannot transform a pin  [?]

ffmpeg tags releases as `n8.1.0`. A consumer that wants the archive named `8.1.0` has no way to say so
without editing the dep's mamafile.

**Proposal:** `add_git(..., version='8.1.0')`. The consumer states the version outright. Both sides read
it from the same place, so §2 cannot recur. Step 1 of §4.3 covers it with no papa.txt change.

This subsumes most of what a mode would buy, and it is simpler to explain. The consumer names the
package, or it does not.

### 5.2 A consumer cannot force identity naming  [?]

A tag that upstream moves should name its package by commit. Today the only way out is to pin the commit
instead of the tag, which also changes what gets checked out.

**Proposal:** `add_git(..., version_from='commit')`, the one mode that earns its place. Do NOT add
`'branch'` (see §7) or `'tag'` (already the default).

Build 5.1 and 5.2 together or not at all. They share the same plumbing, and one without the other leaves
an obvious hole.

### 5.3 A dep cannot compute its own version  [?]

Reading a `VERSION` file, or deriving from a tag inside the dep's own mamafile, still needs code. That is
§6, and P1 and P2 shrank its value. The literal covers a fixed version, the pin covers a released one,
and 5.1 covers a transformed one. What remains is a dep whose version lives in a file only that dep knows
about.

**Recommendation:** build 5.1 and 5.2 first. Then re-read §6 and decide whether the remaining case earns
the cost of executing remote code before a clone.

---

## 6. `def version(self)`, executed in a probe context  [?]

Kept for the case §5.3 describes. Read §5 first: this is the last resort, not the next step.

### 6.1 The Python mechanic that decides the shape  [V]

`BuildTarget.__init__` does `self.version = ''`. An instance attribute **shadows** a subclass method of
the same name, so a mamafile's `def version(self)` would never run while that assignment stays
unconditional. Two ways out:

- **(a) Keep the name `version`.** `__init__` assigns the attribute only when the subclass has not
  defined a callable: `if not callable(getattr(type(self), 'version', None)): self.version = ''`. Then
  every reader goes through one accessor:
  ```python
  def target_version(target) -> str:
      v = getattr(type(target), 'version', None)
      return str((target.version() if callable(v) else getattr(target, 'version', '')) or '')
  ```
  Cost: one conditional in `__init__`, plus one accessor every consumer must use. A mamafile that
  defines the method AND assigns `self.version = x` silently re-shadows it, so P1's scan has to flag
  that combination.
- **(b) A separate hook name**, e.g. `def compute_version(self)`, leaving `version` a plain attribute.
  Simpler Python, no shadowing trap, but two names for one concept.

### 6.2 Running it before a clone  [?]

`fetch_self_version_from_remote` already fetches the mamafile text into a tempdir, and `parse_mamafile`
already imports a mamafile dynamically and instantiates its target class [V]. So the pre-clone probe is:
fetch the text, import it in the tempdir, construct a probe target, call `version()`.

**This executes code from the remote repo before mama has cloned it.** The trust boundary does not
change. Mama clones and executes that same file seconds later, and the consumer's own mamafile named the
url. Only the timing changes. Defining `def version(self)` is the opt-in.

### 6.3 The contract, which the hook's docs must carry

`version()` MUST be:

1. **Deterministic.** The same dep pin and the same args produce the same string, on any machine, with
   no working tree.
2. **Limited to pre-clone inputs.** `self.args`, `self.config`, the dep's pin, and the mamafile's own
   constants. Not the working tree, not the network, not env vars, not the clock.
3. **Side-effect free.** It runs in a probe context. Mama throws away anything it mutates.
4. **Filename-safe and non-empty.** Mama sanitizes the return through `build_names.sanitize_version` and
   raises a clear error on an empty or non-string return.

A `version()` that reads the working tree is the one shape that cannot work, because the pre-clone probe
has no tree. It must fail loudly with that explanation.

### 6.4 Caching

Re-probing per run costs a blob-less fetch per unpinned dep. Persist the resolved version where the
resolved archive name already lives. `dep.write_shim_marker` records the archive [V], and `git_status`
records url, branch, tag and commit, which `check_status` compares [V]. Reuse the stored version while
those stay unchanged.

---

## 7. What NOT to do

- **Do not name a package after a branch alone.** A branch moves, so one name would cover every commit
  ever pushed to it, and a consumer would silently download whichever build landed last. The commit
  stays in the name for exactly this reason.
- **Do not encode a per-consumer variant in the version.** That is what `add_git(args=[...])` is for.
  Args name the archive AND the build dir, and they are known pre-clone by construction.
- **Do not make the version depend on the platform, the arch, the compiler, coverage or sanitizers.**
  Those are separate fields in the archive name already.
- **Do not add a naming input that only a mamafile attribute can set.** No reader can see a mamafile
  attribute before the clone, which is §2 again. A consumer-side `add_git` argument is always safe.
- **Do not require a papa.txt rewrite.** Many packages are already cached with today's records. A change
  that invalidates them is not worth any naming improvement.
- **Do not resolve the version by cloning.** A cache hit must not cost a clone.
- **Do not strip a leading `v` or lowercase a pin.** Both merge distinct tags into one archive name.

---

## 8. Done criteria

- P1: two mamafiles that would previously diverge now either agree or warn, and no upload can publish a
  name the download path cannot construct. **Met.**
- P2: a tag-pinned dep names its archive after the tag with no code and no extra fetch, and a
  branch-pinned dep still gets a new name per commit. **Met.**
- 5.1 and 5.2, step 1: a consumer can name a direct dep's package outright, or force identity naming,
  with no papa.txt change and no cached package touched.
- 5.1 and 5.2, step 2: the same overrides survive a papa.txt hop, and an older mama reading the result
  degrades to a cache miss rather than a wrong download.
- §6: `def version(self)` runs pre-clone, mama caches its result, and every violation of §6.3 fails with
  a message that names the rule it broke.
