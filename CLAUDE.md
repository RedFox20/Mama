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
  `-` instead. An em-dash looks fancy in prose, but it is noise in a source file
  and hard to grep for. The same applies to any other non-ASCII punctuation, such
  as the arrow `→`. Write `->`.
- **Yellow output goes through `warning(text)`** (from `mama.utils.system`),
  not `console(text, color=Color.YELLOW)`. The helper exists so every warning
  passes through one function and looks the same.
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

# GOOD - doesn't fit 130 cols: continue on first line, align under `(`
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

## Git commit style

- Single line, `<type>: <message>` prefix. Examples:
  `feature:`, `fix:`, `refactor:`, `release:`, `cleanup:`.
- No `Co-Authored-By` trailer in this repo. Many other repos want one.
- Atomic commits: one logical change per commit. A bug fix and a refactor go into
  two commits, even in one session.

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

## SSH multiplex / parallel loading

- `mama update` auto-enables `parallel_load`. The `fetch_slot` semaphore caps the
  concurrent git fetches at `parallel_max`, which defaults to 20. This is
  independent of the worker thread count.
- The `SubProcess.run` calls of the shim probe also go through `fetch_slot`. Count
  the slot acquisitions per probe: one for the clone, and one more for `git show`.
- `ensure_master_for_url` is idempotent and serialized per host.

## Tests

- Test directories live under `tests/test_<feature>/`. Each one is a pytest package.
- Mock external IO (subprocess, urlopen, ftplib) heavily. A test must not use the
  network unless it is an integration test (`test_git_pin_change/`,
  `test_papa_deploy/`).
- When you patch, write `patch('mama.<module>.<name>')`. Patch where the code looks
  the name up, not where the code defines it.
- Always run the **full** suite (`python -m pytest tests/`) before you commit. The
  full suite takes about 35 seconds.

### Test code style

The same brevity and DRY rules that apply to `mama/` apply to `tests/`. The old
habit was "tests are throwaway, verbosity is fine". In this repo that habit added
about 13% removable noise to the new test suite. Do not repeat it.

- **Shared stub-builders live in `tests/testutils.py`**, not duplicated per file.
  A second `def _make_dep(tmpdir): config = Mock(); ...` in a new test file is
  duplication. Check `testutils.py` first. Extend or parameterize the existing
  helper. The current `_make_dep` and `_make_target_with_status` duplication across
  6 shim/probe/noart/404 test files is the largest case.
- **Use the pytest `tmp_path` fixture**, not `tempfile.mkdtemp()` with a
  `try / shutil.rmtree() finally`. `tmp_path` is function-scoped, it cleans itself
  up, and it is a `pathlib.Path`. Shorter, no boilerplate, no chance of a leak.
- **No `sys.path.insert(...)` boilerplate** in a test file. `tests/conftest.py` is
  the right place for any test-bootstrap path manipulation.
- **Module docstring: 1-2 lines max, "what this file pins".** The bug background,
  the fix design and the why-this-was-tricky belong in the commit message. Do not
  copy them into the test file docstring. The copy goes stale faster there.
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
it: a one-line fix, a doc edit, an "obviously trivial" diff. The skill exists
because verbosity and duplication appear most often in the changes that looked fine
on first write.

Every task ends with these steps:
1. Implement the change and run the test suite.
2. Invoke `/mama-style-review`, or spawn a sub-agent with the prompt of that skill.
   The skill reports each finding as `<file>:<line> - <rule>: <fix>`.
3. **Apply the fixes.** Do not only acknowledge them. Aim for the line-count
   reduction that the skill targets. That is the success metric, not "all findings
   addressed".
4. Re-run the suite and re-run the review. Loop until `REVIEW PASSED - 0 issues`.
5. **Never commit until a human has reviewed the diff and approved it.** A green
   suite and a passed review are necessary but NOT sufficient. A small quirk, such
   as a wrong guard clause or an over-broad `and not ...` condition, can pass every
   test and still break the feature. Tests do not catch that class of bug, because
   the test carries the same wrong assumption as the code. Present the diff, wait
   for the approval of the human, then commit.

The skill checks: the 130-column limit, no 3+ line single expressions, no break
after `(`, one-liner `if`, no em-dashes, `warning()` instead of `Color.YELLOW`,
`normalized_path()` for paths, `SubProcess.run` over raw `subprocess.run`,
helper-reuse against duplication (in particular against `util.py`,
`utils/system.py` and `tests/testutils.py`), terse test docstrings, dropped
tautological tests, and that a test pins every added behavior.

**Less code means fewer bugs.** A reduction of 30-60% on a refactored file is
normal under these rules. A refactor that does not reduce the line count was too
timid.
