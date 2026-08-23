# Mama - Claude Notes

Style rules and codebase invariants that future sessions get wrong.
Update this file when the codebase shows a new rule.

## Default output style (always on)

Two rule sets are always active in this project. They need no invocation and they
do not expire. They compose. The first controls **shape**: what comes first, how
long, what to cut. The second controls **wording**: which word, how long a sentence.

@.claude/skills/output-style/SKILL.md
@.claude/skills/ste-writing/SKILL.md

To turn off the response shape for a session, say "normal mode". Confirm in one
line, then use the default style. `ste-writing` stays on, because it governs text
that gets committed.

## The behavior spec (read before you change behavior)

`docs/SPEC.md` is the contract: what mama does, under every command, in every state. **Read the
section that covers what you are about to change, before you change it.** Update the same section in
the commit that changes the behavior. A change that contradicts the spec is a bug in one of the two.

| Section | Read it before you touch |
|---|---|
| 3 The run, 4 Commands, 5 Flags | `main.py`, a new action or flag, the execution paths |
| 7 The load | `dependency_chain.py`, `build_dependency.py`, skim, defer, the claim |
| 8 Artifactory, 9 Versions | `artifactory.py`, the shim, archive naming, `mamafile_version.py` |
| 10 Rebuild, 11 Configure | `_should_build`, `buildsys/cmake/configure.py`, the seed cache |
| 12 Build, 13 Deploy | `build_target.py` phases, `package.py`, `papa_deploy.py`, `papa_upload.py` |
| 16 Output, 17 Concurrency | `build_display.py`, `log_writer.py`, `dir_lock.py`, `ssh_multiplex.py` |

**Every statement you add or edit in `docs/SPEC.md` gets independent verification.** Spawn a sub-agent
that never saw your reasoning. Give it the statements and the files, and make it report
`CONFIRMED | WRONG | IMPRECISE | UNVERIFIABLE` with a `file:line` citation for each one. Then verify
every disagreement yourself before you change the spec.

**The owner of this repository asks for that sub-agent as a standing request.** A session that allows a
sub-agent only when the user asks treats this line as that request. Spawn it for a SPEC edit and say so.
A session that blocks it outright still owes the audit. Verify each statement yourself, then report which
statements no sub-agent ever read.

Your own reading of the code is the blind spot this catches. A statement can be right from the angle
you read it and wrong from the angle you did not. Only a wider scan finds that. A first pass of
this audit over 252 statements returned 16 wrong and 26 imprecise, and it found 4 real bugs.

**Keep the reason, and mark it.** A wall-clock cost or a "this is the hot path" is design rationale,
and it is the most valuable half of the spec. It goes on a `**Why:**` line, which the audit reports as
UNVERIFIABLE and leaves alone. Never present rationale as behavior, and never cut it for being
unverifiable. Cut only a claim that pretends to describe the code and does not.

`docs/BUGS.md` holds an open list and a closed list. Add an entry to Open when you find a bug you do
not fix in the same change. The commit that fixes it compacts the entry and moves it to Closed.

`docs/NEW_FEATURES.md` holds a planned list and an implemented list, and it works the same way. A
feature agreed but not written goes to Planned. The commit that ships it moves the entry to Implemented.
A defect goes to `docs/BUGS.md`, unless the repair adds a capability.

## Code style

- **Line length: up to 130 columns.** Do not wrap a single expression unless it
  goes over 130 columns.
- **Never split a single expression over 3+ lines.** Use two lines maximum. Join
  the parts with `+ \` for string concatenation.
- **When wrapping at a `(`, continue on the same line, then align the
  continuation under the character just inside the opening parenthesis.** Do NOT
  break right after `(`.
- **One-liner `if` for a single short statement.** Use `if cond: do_thing()` on
  one line when the body is a single short call.
- **No em-dashes (U+2014) in code, comments, or docs.** Use a regular ASCII dash
  `-` instead. Non-ASCII punctuation is noise in a source file and hard to grep
  for. The same applies to the Unicode arrow (U+2192). Write `->`.
- **Yellow output goes through `warning(text)`** (from `mama.utils.system`),
  not `console(text, color=Color.YELLOW)`. One function, one look for every
  warning.
- **Cache a repeated or invariant calculation.** Never compute the same value
  twice. A `sum()`, probe, `.encode()` or `stat` evaluated twice in one
  expression (for example in a filter AND in the value it builds) is a finding.
  So is a value recomputed on each loop iteration when the loop does not change
  it. Hoist it out and compute it once. A result that stays constant for the
  whole process (terminal encoding, cpu count, a compiled regex) gets memoized
  one time, not re-derived per call. Less work and less code, same result.

### Examples

```python
# GOOD - single short statement, one-liner
if dep.config.verbose: error(f'  {dep.name: <16} {msg}')

# BAD - 2 lines for a single short statement
if dep.config.verbose:
    error(f'  {dep.name: <16} {msg}')

# GOOD - fits in 130 cols, one line
raise RuntimeError(f'papa_deploy refused: {package_full_path} contains a mama_shim marker.')

# BAD - 3 lines for an expression that fits
raise RuntimeError(
    f'papa_deploy refused: {package_full_path} contains a mama_shim marker.'
)

# GOOD - does not fit 130 cols: continue on first line, align under `(`
raise RuntimeError(f'Target {dep.name} requires network to clone but network is unavailable.' + \
                   ' Check your connection or use a cached artifactory package.')

# GOOD - same pattern with implicit string concat
console(f'{indent}Artifactory CACHE (size-match) '
        f'{os.path.basename(local_file)} ({get_file_size_str(size)})')

# BAD - break after opening paren
raise RuntimeError(
    f'Target {dep.name} requires network to clone but network is unavailable.'
    f' Check your connection or use a cached artifactory package.')
```

## Platform handling (read the doc before you touch it)

Every target platform is one class under `mama/platforms/`, reached through a single
`config.platform`. Build system logic lives in `mama/buildsys/`, never in a platform.
No consumer chains over platform names: what it needs, it declares on `Platform` and
reads back. `tests/test_platforms/test_layering.py` fails the build if a platform
imports a build system or names a `CMAKE_` variable.

**Read the architecture summary before you add a platform, add a compiler flag or a
cmake option, or edit `build_config.py`, `buildsys/cmake/options.py` or `mamacmake.py`.**
A change that adds a platform branch to a consumer is the wrong change.

@docs/platforms.md

## Path handling - forward slashes everywhere

The project uses forward slashes on every platform, including Windows. The
utility is `mama.util.normalized_path()`. It calls `os.path.abspath`, then
`.replace('\\', '/')`.

- Some functions return a backslash path, in particular
  `tempfile.TemporaryDirectory()` on Windows. Pass the result through
  `normalized_path()` BEFORE you put it into a shell command string.
- `shlex.split()`, which `SubProcess` uses, reads a backslash as an escape. A raw
  Windows path in a command string corrupts without a warning.
- For directory cleanup on Windows, use `tempfile.TemporaryDirectory(prefix='...',
  ignore_cleanup_errors=True)`. Git leaves read-only files in `.git/objects/` that
  make `shutil.rmtree` fail.

## Subprocess: the two-tool rule

There are two primitives. They are NOT interchangeable.

- **`SubProcess.run(cmd, cwd=, io_func=, timeout=)`** - the standard wrapper of
  this project. It uses `subprocess.Popen` with `pty.openpty()` on UNIX, so the
  child gets a real TTY for the git progress output. On Windows it uses plain
  `Popen` with pipes. It is multi-thread safe and it has a timeout. **Use this
  for everything by default.**
- **`subprocess.run(...)` directly** - only for the rare case that must suppress
  stderr (`stderr=subprocess.DEVNULL`) and keep a timeout, but does not want the
  live progress UI. The current example is the post-blob:none `git show
  HEAD:<file>` in `Git.fetch_self_version_from_remote`. Its lazy fetch prints
  `remote: ...` lines that must not reach the user.

When you deviate from `SubProcess.run`, document why in the function docstring.

**Never** use `os.system("cd <dir> && cmd")`. `SubProcess.run(cmd, cwd=<dir>)` is
the correct idiom. SubProcess uses `execve`, not a shell, so `cd` and `&&` are not
valid.

**Never** use `os.forkpty()` directly anywhere in this codebase. Python 3.12 marks
it as unsafe in a multi-threaded program, and mama runs many threads in parallel.

## GitHub review comments

A reviewer comment is a thread, and a thread stays open until someone closes it. **Answer every one,
and close the ones you fixed.** A reader who opens the pull request has to see which findings are
gone and which are still live, without reading the diff.

### Verify the premise before you write the fix

**A review comment is a hypothesis, not an instruction.** Read the code it names, and run the case it
describes, before you change a line. A reviewer sees a diff, never the whole call chain. So a finding
names a real risk and still gets the mechanism, the remedy or both wrong.

Check these three, in order:

1. **Does the failure the comment describes actually reach that line?** An earlier guard often
   rejects the input first, and a fix below it is dead code.
2. **Does the proposed remedy work?** Name what it changes, then prove that it changes it. A remedy
   that reads one file misses a value the run also gets from another.
3. **What else does the remedy catch?** A wider predicate fires on the healthy case too. Measure the
   normal case before you widen anything.

A fix that passes its own new test still regresses the build when the premise was wrong. Three
findings in one pull request proved this. One named a path an earlier check already rejected. One
offered two remedies that both failed a measurement. One widened a scan until two real module builds
broke. Reply with the measurement either way. It closes a wrong finding and it strengthens a right one.

### Then answer, in one of three ways

1. **You fixed it.** Reply with the commit hash, one sentence on what the code does now, and the test
   that pins it. Then resolve the thread.
2. **The reviewer is right, and the fix is not in this pull request.** Reply with that, name the entry
   you added to `docs/BUGS.md`, and leave the thread open.
3. **The reviewer is wrong, or the fix would be wrong.** Reply with the reasoning and the `file:line`
   that proves it. **Leave the thread open**, because the author decides, not you.

Never resolve a thread you did not answer, and never resolve one you argued against.

```bash
# every thread, with its resolved state and its first comment
gh api graphql -f query='{ repository(owner:"OWNER", name:"REPO") { pullRequest(number:NN) {
  reviewThreads(first:50) { nodes { id isResolved comments(first:1){nodes{databaseId path body}} } } } } }'

# reply to one thread, by the databaseId of its first comment
gh api -X POST repos/OWNER/REPO/pulls/NN/comments/<databaseId>/replies -f body='Fixed in <sha>. ...'

# resolve it, by the thread id
gh api graphql -f query='mutation { resolveReviewThread(input:{threadId:"<id>"}) { thread { isResolved } } }'
```

A `@codex review` request answers a commit, so push first, then request the review. A finding that
survives two rounds needs a different fix, not a third round of the same one.

## Git commit style

- Single line, `<type>: <message>` prefix. Examples:
  `feature:`, `fix:`, `refactor:`, `release:`, `cleanup:`.
- No `Co-Authored-By` trailer in this repo. Many other repos want one.
- Atomic commits: one logical change per commit. A bug fix and a refactor go into
  two commits, even in one session.

## changelog.txt

`changelog.txt` in the repo root lists every release, newest first. **Update it when
you finish a feature or a bug fix, in the same commit that carries the change.** A
changelog written at release time from the git log is guesswork.

New entries go under the `unreleased` heading at the top. The release step renames
that heading to the version and the date.

```
release: 0.13.10 (2026-Aug-04)
 - feature: minimal description of feature 1
 - bugfix: minimal description of a fixed bug
```

- **80 columns max per line**, and one line per entry. No wrapped entries.
- Prefixes: `feature:`, `bugfix:`, `perf:`, `refactor:`, `build:`.
- Newest release first. Date format `YYYY-Mon-DD`.

**Summarize hard. You have permission to drop detail.** A changelog is a list of
general bullet points, not an essay. One architectural change is ONE bullet, however
many commits, files or lines it took. A 5000 line diff that adds one capability earns
one line. Nobody reads a changelog to learn which function moved.

- Write for a USER of mama: what can they now do, or what no longer breaks.
- Merge related commits. Ten commits that build one feature are one entry.
- Drop anything a user cannot observe: refactors, test changes, doc edits, internal
  renames, review and style work. If it changed no behavior, it earns no line.
- Prefer 3 to 6 entries per release. More than 8 means you are transcribing the git
  log instead of summarizing it.

## Release process

1. Bump the patch version in `mama/_version.py`. Bump the minor version only when
   the user asks for it.
2. Rename the `unreleased` heading in `changelog.txt` to
   `release: {major}.{minor}.{patch} (YYYY-Mon-DD)`. Add the entries any commit
   missed.
3. Copy the new release into the `## Recent changes` section of `README.md`. Drop the oldest
   one there. PyPI has no field for release notes, so that section is the only place the
   entries reach the project page. `tests/test_release_metadata/` fails when the README and
   `changelog.txt` disagree.
4. Run both gates. Release only when both pass. The full suite is
   `python -m pytest tests/`. The slow gate is `python -m pytest tests/ -m slow`, which
   names the whole tree, so a slow test in a new dir runs too. The default run excludes the
   slow gate, so a release that skips it ships a toolchain nobody configured.
5. On Windows, run both gates again inside WSL. A Windows-only run has shipped a broken
   Linux build. The mirror lives at `~/Mama`. Clone it there when it is missing:
   `git clone git@github.com:RedFox20/Mama.git ~/Mama`. Sync it to the release commit
   first, because a stale mirror tests the wrong tree.
6. Commit: `release: v{major}.{minor}.{patch} <50 char description>`.
7. Push the release commit.
8. Run `./deploy.sh` to publish the build to PyPI. Prefer the WSL mirror, because
   `~/.pypirc` there holds the token and twine asks nothing. On Windows twine reads the
   token through keyring, which opens a dialog. A background shell cannot answer a dialog,
   so it hangs with no output until it times out.

Steps 7 and 8 reach outside this machine, so ask the user before you run them.

`twine upload` passes `--skip-existing`, so a repeated deploy is safe.

## Artifactory + git status invariants

- **A 404 from artifactory for a git dep is NORMAL.** It means there is no prebuilt
  package for the current commit. The 404 must NOT wipe the `git_status` file. If
  the status is wiped, the next `mama update` reads an empty status. `check_status`
  then reports "SCM change detected" and forces a full rebuild. `check_status`
  already detects a real url, tag, branch or commit change by direct comparison.
- A 404 IS fatal for `is_pkg` deps. Those URLs are mandatory.
- The shim probe (`try_load_artifactory_shim`) only runs when there is no existing
  working tree (`not self.is_real_clone()`). For an already-cloned dep, the regular
  `fetch + reset` path is correct. The extra probe only re-clones into a tempdir
  and does nothing useful.
- **A targeted run stays inside the subtree of its target.** The load runs in two
  stages. Stage one, `load_path_to_target`, reads the cheapest dep next and stops
  the moment the graph names the target. A local dep costs one mamafile parse, so it
  reads first, and a branch the walk never enters stays unread. This walk is serial
  on purpose. A parallel wave would read the branches the early stop exists to skip.
  Inside it, `_defer_load` skips every network step of a dep outside the target,
  which is the shim probe, the package fetch and the clone. **Exploring the graph
  must never turn a cached shim into a clone.** A dep keeps its name while deferred,
  so `find_dependency` still finds it. Stage two, `revive_deferred_target_deps`,
  loads the subtree of the target and nothing else. When the graph never names the
  target, the cached packages expand first, because they cost no network. Only then
  do the deps that need a fetch expand.
- **A skim names children, it does not load a dep.** `BuildDependency.skim` parses the
  mamafile and runs `settings()` and `dependencies()`, because only those two hooks name
  a child. It creates no build dir. Both hooks run once, so `_load` must skip them when
  `did_skim` is set. A second `dependencies()` call makes `add_child` refuse a child it
  already holds. While a skim runs, `build_dir()` and `source_dir()` raise, so a mamafile
  that reads a path too early fails fast instead of writing outside the dependency.
- **EVERY action that names a target executes that subtree alone**, not only build,
  upload and deploy. An out-of-scope dep builds nothing, yet it still reaches
  `_run_packaging`, where a mamafile asserts on libs that no run produced.

## SSH multiplex / parallel loading

- Parallel load is the default for every run, and `serial` opts out. The `fetch_slot`
  semaphore caps concurrent git fetches at 8 (`DEFAULT_MAX_CONCURRENT_FETCHES`),
  whatever `parallel_max` asks for. `parallel_max` (default 20) sizes the scheduler's
  LOAD pool. Both are independent of the worker thread count.
- The `SubProcess.run` calls of the shim probe also go through `fetch_slot`. Count
  the slot acquisitions per probe: one for the clone, and one more for the ls-remote
  that resolves the commit. The `git show` reads the clone the probe already made,
  so it takes none.
- `ensure_master_for_url` is idempotent and serialized per host.
- **One thread owns each dep's load.** `load_dependency_chain` claims a dep before it loads
  it, so a shared dep loads once, walks its subtree once and draws one display line. A parent
  that arrives at a claimed dep waits for the answer and walks nothing. A parent that arrives
  at a dep an earlier walk finished returns at once. The lock covers the claim, never the load.
- **The walk always enters the dep it starts from.** mamabuild loads the root first, and a
  reload revives deps below a scope that already loaded. Every OTHER loaded dep stops the walk.
- **mamabuild owns the run.** It loads the root, then opens the one build log under the
  workspace that root named. A display reads that log through `get_build_log`. It opens none.
  The classic path wraps its whole load in one `load_display` region, which closes before the
  package listing prints. `execute_unified` draws its own.

## Tests

- Test directories live under `tests/test_<feature>/`. Each one is a pytest package.
- Mock external IO (subprocess, urlopen, ftplib) heavily. **No test uses the network.**
  The git integration tests clone the local bare repo the `example_remote` fixture
  builds, so they stay fast and they never fail on a flaky connection.
- When you patch, write `patch('mama.<module>.<name>')`. Patch where the code looks
  the name up, not where the code defines it.
- **Patch a mama function with `autospec=True`.** A plain `Mock` accepts any argument,
  so a call that names a keyword the real function does not have still passes. That gap
  shipped `execute(cmd, exit_on_fail=False)`, which only a consumer CI caught.
- Always run the **full** suite (`python -m pytest tests/`) before you commit. It runs on 8
  worker processes and takes about 5 seconds on Linux, 13 on Windows. Add `-n0` to debug one
  test or to read a traceback in order. It also lets a profiler see what a test spawns.
  `pip install -e .[dev]` installs the pytest-xdist that the 8 workers need.
- **To find out why something is slow, run `bench/profile_mama.py`.** It counts the
  child processes a run spawns and marks any that averages over 0.5 seconds. mama is
  IO bound, so that table answers the question more often than a profiler does. Its
  docstring explains all three modes.

  ```
  python bench/profile_mama.py census pytest tests/test_git_pin_change/
  python bench/profile_mama.py census mama build
  python bench/profile_mama.py tests        # the slowest tests
  ```

### Test code style

The same brevity and DRY rules that apply to `mama/` apply to `tests/`. Tests are
not throwaway code, so verbosity is not fine there either.

- **Shared stub-builders live in `tests/testutils.py`**, not duplicated per file.
  A second `def _make_dep(tmpdir): config = Mock(); ...` in a new test file is
  duplication. Check `testutils.py` first. Extend or parameterize the existing
  helper.
- **Use the pytest `tmp_path` fixture**, not `tempfile.mkdtemp()` with a
  `try / shutil.rmtree() finally`. `tmp_path` is a function-scoped `pathlib.Path`
  that removes itself. Shorter, no boilerplate, no chance of a leak.
- **No `sys.path.insert(...)` boilerplate** in a test file. `tests/conftest.py` is
  the right place for any test-bootstrap path manipulation.
- **Module docstring: 1-2 lines max, "what this file pins".** The bug background
  and the fix design belong in the commit message, not in the docstring, where
  the copy goes stale faster.
- **No class docstring that paraphrases what every test in the class checks.** The
  test method names and their assertions already say it.
- **Per-test docstrings only when an unusual invariant needs an explanation.** Do
  not write `"""The bug: a 404 fetch was deleting git_status..."""` above
  `def test_404_does_not_wipe_git_status`. The name already says it.
- **Comments explain WHY, not WHAT.** This is the same rule as for `mama/` code.
  The assertion already says what. Add a comment only when the choice surprises a
  reader. For example, explain why the code treats an `ls-remote` failure as "cache
  still fresh" and not as "drop the cache".
- **Scope a patch to the smallest block that needs it.** A repeated `with patch(...)`
  setup across tests in one file is a fixture or helper opportunity.

## The work cycle (default behavior for every change)

```
Edit -> Review -> Refactor -> Test -> (Edit) -> Review  [until 0 issues]
```

**This loop is the default behavior, not an option.** Every change set goes through
it: a one-line fix, a doc edit, an "obviously trivial" diff. Verbosity and duplication
appear most often in the changes that looked fine on first write.

**Run the review at every hand-off, not only at the end of a task.** Before you answer
the user with work in the tree, and while the test suite runs. Its step 0 re-reads
`ste-writing` and `output-style` from disk, because both drift out of a long session.
A `Stop` hook in `.claude/settings.json` says the same thing when the tree holds
uncommitted changes.

**The review never runs in the planning phase.** Planning writes no code, so there is no
diff to review. The file `.claude/.planning` marks the phase. While it exists, the `Stop`
hook stays quiet and you must not start the review. Create it when a session turns to
planning, and delete it when you write the first line of code:

```
touch .claude/.planning     # planning starts
rm .claude/.planning        # implementation starts, the review is live again
```

Another agent may hold uncommitted work in the same tree. Review only what you wrote.

Every task ends with these steps:
1. Implement the change, then start the test suite. It takes about 5 seconds on Linux, so run it
   as often as you like.
2. Invoke `/mama-style-review` WHILE the suite runs, or spawn a sub-agent with the prompt of
   that skill. The skill reports each finding as `<file>:<line> - <rule>: <fix>`. Read the suite
   result when it lands. Re-run the tests a change touched after you apply the fixes.
3. **Apply the fixes.** Do not only acknowledge them. Aim for the line-count
   reduction that the skill targets. That is the success metric, not "all findings
   addressed".
4. Re-run the suite and re-run the review. Loop until `REVIEW PASSED - 0 issues`.
5. **Never commit until a human has reviewed the diff and approved it.** A green
   suite and a passed review are necessary but NOT sufficient. A wrong guard clause
   or an over-broad `and not ...` condition can pass every test and still break the
   feature. The test carries the same wrong assumption as the code. Present
   the diff, wait for the approval of the human, then commit.

The skill checks: the 130-column limit, no 3+ line single expressions, no break
after `(`, one-liner `if`, no em-dashes, `warning()` instead of `Color.YELLOW`,
`normalized_path()` for paths, `SubProcess.run` over raw `subprocess.run`,
helper-reuse against duplication (in particular against `util.py`,
`utils/system.py` and `tests/testutils.py`), terse test docstrings, dropped
tautological tests, and that a test pins every added behavior.

It also checks the shape of the answer that reports it, against `output-style`: the next action
first, no preamble, no recap, no closing pleasantry, lists capped at five.

It also runs the `ste-writing` lint over the prose the diff adds - every
docstring, comment, console string and exception message: no contractions, no
semicolon in prose, no non-ASCII punctuation, sentences under 20 words, active
voice, plain verbs, no idiom, one name for one thing. A code comment gets the
same standard as an error string, because it ships with the code. This rule set
slips most often, so the greps in the skill are not optional.

**Less code means fewer bugs.** A reduction of 30-60% on a refactored file is
normal under these rules. A refactor that does not reduce the line count was too
timid.
