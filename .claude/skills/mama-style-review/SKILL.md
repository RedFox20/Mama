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
