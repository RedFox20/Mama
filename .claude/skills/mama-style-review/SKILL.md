---
name: mama-style-review
description: >
  Mandatory final-stage review of pending changes against the project's
  CLAUDE.md style and reuse rules. Run after every change session before
  considering any feature complete and before committing. Loops
  fix-and-re-review until 0 issues remain.
---

# Mama Style + Reuse Review

You are reviewing pending changes in the Mama project against the rules in
[CLAUDE.md](../../CLAUDE.md). **No feature is considered complete until this
review passes with 0 issues.**

The user has explicitly opted into this being the final stage of every task.
Run automatically as the last todo item; loop until clean.

## The work cycle (default behaviour for every task)

```
Edit -> Review -> Refactor -> Test -> (Edit) -> Review  [until 0 issues]
```

This is not optional, not "nice to have", not "for big changes". Every change
set goes through this loop, including one-line fixes, including doc edits,
including "trivial" diffs that look correct on first write. Verbosity and
duplication appear most often exactly in the changes that "looked obviously
fine". The cycle catches them.

## Line count is a primary metric

**Less code means fewer bugs.** When applying review findings, the success
metric isn't "fixed the listed issues" - it's "net line count went down,
materially". When the review finds duplication or verbose docstrings or
boilerplate fixtures, the target is typically a **30-60% reduction** in the
affected file. If a refactor doesn't move the line count meaningfully, the
refactor was too timid.

Concrete examples from this codebase (production reductions, all behaviour-preserving):
- `test_noart_shim_cache.py`: 256 -> 110 lines (-57%)
- `test_shim_load_integration.py`: 181 -> 64 lines (-65%)
- `test_shim_guards.py`: 290 -> 138 lines (-52%)
- `test_shim_probe.py`: 184 -> 81 lines (-56%)
- `test_artifactory_404_status.py`: 131 -> 59 lines (-55%)

Net across that pass: 16 files changed, **499 insertions / 1135 deletions**.
260 tests still pass. The reductions came from the patterns documented below;
the review skill exists to find more like them.

## What worked (patterns to apply, not just to flag)

When you find a violation, prefer these proven moves:

1. **Hoist shared stub-builders into `tests/testutils.py`** (or `mama/util.py`
   for production helpers). A second `def _make_dep(...)` in a new file is a
   loud signal to extend the shared helper instead. Parameterise via
   `**overrides` rather than copying.

2. **Use pytest's `tmp_path` fixture** in place of `tempfile.mkdtemp() +
   try/finally + shutil.rmtree(...)`. It's function-scoped, auto-cleaning,
   and gives you a `pathlib.Path`. Saves 5-6 lines per test method.

3. **`sys.path` bootstrap lives in `tests/conftest.py`, once.** Strip it from
   every test file. One conftest line ate ten test files of boilerplate.

4. **Module docstrings: 1 line.** The bug background, the fix design, the
   why-this-was-tricky - all of that goes in the commit message. The test
   file's docstring answers "what does this pin" in a sentence.

5. **Drop tautological tests.** An assertion that can't fail regardless of
   the code under test is noise. Example flagged this pass:
   `test_load_does_not_set_did_check_artifactory_on_shim_miss` whose docstring
   literally admitted it didn't really test anything. Delete it.

6. **Comments explain WHY, never WHAT.** `# Marker still intact.` above
   `assert dep.is_artifactory_shim()` adds nothing - the assertion is already
   self-describing. Keep comments only when the choice would surprise a
   reader (e.g. why ls-remote failure is treated as "cache fresh" not
   "cache stale").

7. **Inline trivial helpers; extract repeated ones.** Three identical patch
   blocks across three tests = factor out. A single-use lambda used once =
   inline. Aim for the median test method to fit in 5-10 lines.

8. **Collapse multi-line single expressions** that fit on one or two lines.
   `subprocess.Popen(\n  args, cwd=cwd, env=env,\n  stdin=PIPE, ...\n)`
   broken across 6 lines is wrong when 2 lines fits 130 cols.

9. **Class docstrings paraphrasing test methods - delete.** If
   `class TestX` summarises what `test_x_does_y` already says by name, the
   class docstring is noise.

10. **Per-test docstrings only when an unusual invariant needs explaining.**
    `test_404_does_not_wipe_git_status` does not need
    `"""The bug: a 404 fetch was deleting git_status..."""` - the name says it.

## How to run

1. **Inspect pending changes.** Combine staged + unstaged:
   ```
   git diff --staged
   git diff
   git status --short
   ```
   Identify the set of changed/new files in `mama/`, `tests/`, `CLAUDE.md`, `README.md`.

2. **Re-read CLAUDE.md from disk.** Don't trust memory - the rules evolve.

3. **Mechanically check every rule below** against the diff. Track findings.

4. **Loop:** if findings, fix them, then re-run from step 1. Stop only when
   the review reports 0 issues. Do NOT proceed to commit if any rule fails.

## Hard rules (must pass)

### Formatting
- **130-col line limit.** Lines that fit must not wrap.
- **No 3+ line single expressions.** Two lines max, joined with `+ \` for string
  concatenation. Look for `f-string\n f-string` patterns (implicit-concat split
  across many lines) and collapse.
- **Never break right after `(`.** Continuation must start on the same line as
  the opening paren, then subsequent lines align under the character just
  inside that paren.
- **One-liner `if`** for a single short statement: `if cond: do_thing()`. Two
  short statements separated by an `if cond:` block on their own lines is a smell.
- **No em-dashes** (the long dash, Unicode U+2014) anywhere - code, comments,
  docstrings, markdown. Use ASCII `-`. This SKILL.md only mentions the character
  by name to define the rule; the character itself does not appear in this file.

Grep helpers:
```bash
grep -rn "$(printf '\xe2\x80\x94')" mama/ tests/ CLAUDE.md README.md
awk 'length>130' mama/**/*.py     # over-long lines
```

### Yellow output convention
- All warning-style yellow console output goes through `warning(text)`
  (from `mama.utils.system`), NOT `console(text, color=Color.YELLOW)`.
- Migration is complete; flag any new `Color.YELLOW` use.

```bash
grep -rn 'Color\.YELLOW' mama/ | grep -v 'utils/system.py'
```

### Paths
- All paths are forward-slash on every platform. Anything that may return a
  backslash path (notably `tempfile.TemporaryDirectory()` on Windows) must be
  passed through `normalized_path()` before interpolating into a shell command.
- For temp dirs used by git: `ignore_cleanup_errors=True` (Python 3.10+).

### Subprocess
- Use `SubProcess.run(cmd, cwd=, io_func=, timeout=)` by default - it's the
  project's standard, multi-thread safe.
- Direct `subprocess.run(...)` is only acceptable when you specifically need
  `stderr=DEVNULL` and a timeout but don't want the live progress UI. The
  function docstring MUST document the why.
- **Never** `os.system("cd <dir> && cmd")` - use `cwd=` on `SubProcess.run`.
- **Never** `os.forkpty()` - unsafe in multi-threaded programs.

### Duplication / reuse
- Before introducing a helper, grep the codebase for an existing one with the
  same intent. Common haunts:
  - `mama/util.py` - paths, file io, time strings, downloads.
  - `mama/utils/system.py` - `console`, `error`, `warning`, `get_colored_text`.
  - `mama/utils/sub_process.py` - subprocess primitives.
- Hardcoded literals that already exist as named constants must use the constant:
  - `'mama_shim'` → `MAMA_SHIM_FILENAME` (from `mama.util`)
  - Use `has_shim_marker(path)` for the directory existence check.
- A new ~3-line helper duplicating something in util.py is a finding.

### Code shape
- Long functions are a smell. If you find yourself adding a third large
  responsibility to a function (e.g. another inline artifactory probe inside
  `_load`), extract a helper.
- Preserve existing structural patterns. E.g., `_load`'s `if self.is_root:
  ... else: ...` early-branch pattern - don't add code after that branch when
  it logically belongs inside the non-root branch.
- Avoid re-checking conditions the parent branch already proved. If the
  surrounding `if not self.is_root` makes `is_root` False for the block,
  don't re-test it in nested conditions.

### Tests
- Every new feature / bug fix needs at least one test that pins the new
  behaviour. No exceptions; this is enforced by the wider workflow but
  the review must call it out if missing.
- Mock external IO (subprocess, urlopen, ftplib). Tests must not hit the
  network unless integration-flavored.
- When patching: `patch('mama.<module>.<name>')` - patch where the name is
  LOOKED UP, not where it's defined.

### Test verbosity / duplication (specific patterns to flag)

The same brevity rules apply to tests. These patterns sneaked in across the
new shim/probe/noart/404/sub_process test files and must not return:

- **Duplicate `_make_dep` / `_make_target` helpers** across multiple test
  files. Look in `tests/testutils.py` first; extend that. Flag any
  per-file stub-builder that mirrors another file's.
  ```bash
  grep -rn 'def _make_dep\|def _make_target\|def _make_shim' tests/
  ```
  More than one site of the same intent = finding.

- **`tempfile.mkdtemp() ... try ... finally: shutil.rmtree(...)`** patterns
  in test methods. Use pytest's `tmp_path` fixture instead - it's
  function-scoped, auto-cleans, and is a `pathlib.Path`.
  ```bash
  grep -rn 'tempfile.mkdtemp\|shutil.rmtree' tests/
  ```

- **`sys.path.insert(...)`** at the top of test files. Belongs in
  `tests/conftest.py`, exactly once.
  ```bash
  grep -rn 'sys\.path\.insert' tests/
  ```

- **Module docstring longer than 2 lines.** Background/history belongs in
  the commit message, not the test file. The docstring should answer
  "what does this file pin?" in a sentence.

- **Class docstrings that paraphrase the test methods.** If
  `class TestX` has a docstring that summarises what every
  `test_x_does_y` method already says by name, delete the class docstring.

- **Per-test docstrings that just re-English the test name.**
  `test_404_does_not_wipe_git_status` with docstring "The bug: a 404
  fetch was deleting git_status..." - the name already says it. Keep
  docstrings only when there's a subtle invariant or counter-intuitive
  expectation to explain.

- **Comments that narrate WHAT the assertion checks.**
  `# Marker still intact.` above `assert dep.is_artifactory_shim()` -
  the assertion is already self-describing. Comments only earn their
  keep when they say WHY (e.g. why we treat ls-remote failure as
  "cache fresh" instead of "cache stale").

- **Repeated `with patch(...)` setup across tests in the same file.**
  Extract to a fixture or helper method when the same three patches
  appear three or more times.

### Commit style
- Single-line `<type>: <message>`. Types: `feature`, `fix`, `refactor`,
  `release`, `cleanup`, `docs`. (Note: it's `feature`, NOT `feat`.)
- No `Co-Authored-By` trailer.
- Atomic commits - one logical change per commit.

## Reuse-detection workflow

For any new helper added to a file:
1. `grep -rn "def <similar_name>" mama/` - is there already a function doing this?
2. `grep -rn "<distinctive_implementation_line>" mama/` - is the implementation
   pattern already used elsewhere inline that could now share the helper?
3. If duplicate intent exists - either reuse, or extract a single shared
   utility (typically in `util.py` or `utils/system.py`).

## Output format

Report findings as a numbered list, each entry:
```
N. <file>:<line> - <rule>: <what's wrong> → <suggested fix>
```

When 0 issues: respond with `REVIEW PASSED - 0 issues`. Then the calling
context may proceed to commit.

When >0 issues: respond with the list, then fix each. After fixing, re-run
the entire review from step 1. Do NOT skip the re-run - fixes often introduce
new violations.

## Reminders

- Run the full test suite (`python -m pytest tests/`) before declaring done.
- Tests must pass deterministically (run twice if needed - flaky tests are a
  separate concern but block the commit).
- The review must be invoked even when changes look "obviously trivial" -
  trivial changes still routinely violate the 130-col rule or sneak in an em-dash.
