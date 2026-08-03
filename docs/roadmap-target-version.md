# The version field: how it works now, and what is left to build

**Status:** P1 and P2 have landed (§3). §4 records a correction. §5 explains why no further consumer
argument is needed. §6 is the only unbuilt item, and it is deliberately parked.
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

`self.version` covers a third-party dep too, because `add_git(mamafile='mamadeps/x.py')` points at a
file in the CONSUMER's repo, which the reader can open before any clone. See §5.1.

### 1.1 The rules, and the constraint each one serves

1. **`self.version` must be ONE string the reader can resolve.** That is a literal, or a module-level
   constant bound once to a literal. The reader cannot evaluate an f-string, a call, or a name whose
   binding depends on what ran.
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

The scan PARSES the mamafile, and falls back to a line scan only when `ast.parse` fails. A line scan counts a docstring that
documents `self.version` as an assignment, and an error message that quotes it too. It then refuses the
real pin next to it. Parsing costs about 0.14ms on a real mamafile, against a probe
path that already spends 100ms or more on the network.

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

## 4. Correction, and the decision not to use it  [V]

An earlier draft claimed a consumer-side `version_from` could not work, because `DepSource.papa_join` is
positional and a sixth field would make every older papa.txt misparse its args. **That reasoning was
wrong.** It assumed a positional field. A KEYED token in the args tail costs nothing, measured against
the CURRENT parser [V]:

```
qcoro,https://x/qcoro.git,,v0.13.0,mamadeps/qcoro.py,version_from=commit,LGPL
```

| case | result |
|---|---|
| new mama reads an OLD papa.txt | identical to today. No `version_from=` token is present, so nothing changes |
| new mama reads a new papa.txt | reads the token, and keeps the remaining tokens as args |
| **old** mama reads a new papa.txt | the token leaks into `args`, so that child names itself `...-versionfromcommit-...` and misses its cache |

So the mechanism exists, and no cached package would need a rewrite.

**Mama is not going to use it.** §5 shows that every case a mode would serve already has an answer. An
unused mode is one more naming input a future reader must understand. Keep this section as the record of
how a keyed field would work, in case a real case appears.

## 5. Why no consumer-side version argument is needed  [V]

Two cases looked like they needed one. Both already have an answer, and neither answer touches papa.txt.

### 5.1 "The upstream tag is not the name I want"

ffmpeg tags releases as `n8.1.0`. A consumer that wants the package named `8.1.0` writes it in the
override mamafile it already owns:

```python
# the consumer's own mamafile
self.add_git('ffmpeg', 'https://git.ffmpeg.org/ffmpeg.git', mamafile='mamadeps/ffmpeg.py', git_tag='n8.1.0')

# mamadeps/ffmpeg.py, a file in the CONSUMER's repo
class ffmpeg(mama.BuildTarget):
    def settings(self):
        self.version = '8.1.0'
```

`mamadeps/ffmpeg.py` sits on disk before any clone, so the pre-clone reader finds it [V]. That is why
`fetch_self_version_from_remote` returns None when `dep.mamafile` is set. The local override was already
read, and the remote repo's own mamafile is not the one mama runs.

`self.version` beats the tag in the §1 table, so the package is named `8.1.0`. No new argument, no
papa.txt field, and the rule a reader has to learn is one they already know.

Pinned by `test_a_consumer_owned_override_mamafile_names_the_package`.

### 5.2 "This tag moves, so name it by commit"

Pin the commit. `add_git(..., git_commit='4acd905...')` names the package by its short hash AND checks
out what you meant. A tag that upstream re-points is not a stable thing to track. Tracking it while
naming by identity asks for one thing and means another.

### 5.3 What genuinely remains: a dep that computes its own version  [?]

Reading a `VERSION` file, or deriving a version inside the dep's own mamafile, still needs code. That is
§6. P1, P2 and 5.1 shrank its value. The literal covers a fixed version, the pin covers a released one,
and an override mamafile covers a transformed one. What remains is a dep whose version lives in a file
that only that dep knows about, and whose mamafile the consumer does not own.

**Recommendation:** leave §6 unbuilt until such a dep actually appears. Every case seen so far is
already covered.

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
- **Do not add a `version_from` mode.** Every case it would serve has an answer in §5. An unused naming
  input is one more rule a reader must learn before they can predict an archive name.
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
- 5.1 and 5.2: no code needed. An override mamafile and a commit pin already cover both. **Met.**
- §6: `def version(self)` runs pre-clone, mama caches its result, and every violation of §6.3 fails with
  a message that names the rule it broke.
