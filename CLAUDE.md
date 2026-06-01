# Mama - Claude Notes

Hand-written notes for Claude. Capture style rules and codebase invariants that
keep biting future-Claude. Update as the codebase teaches new lessons.

## Code style

- **Line length: up to 130 columns.** Don't wrap a single expression unless it
  actually exceeds 130 cols.
- **Never split a single expression over 3+ lines.** Two lines max, joined with
  `+ \` for string concatenation.
- **When wrapping at a `(`, continue on the same line, then align the
  continuation under the character just inside the opening parenthesis.** Do NOT
  break right after `(`.
- **One-liner `if` for a single short statement.** Use `if cond: do_thing()` on
  one line when the body is a single short call.
- **No em-dashes (`-`) in code, comments, or docs.** Use a regular ASCII dash
  `-` instead. Em-dashes look fancy in prose but are noise in source files and
  hard to grep for.
- **Yellow output goes through `warning(text)`** (from `mama.utils.system`),
  not `console(text, color=Color.YELLOW)`. The helper exists so warnings have
  a single chokepoint and a consistent shape.

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

## Path handling - forward slashes everywhere

The project standardises on forward slashes on every platform, including
Windows. The utility is `mama.util.normalized_path()` (which calls
`os.path.abspath` then `.replace('\\', '/')`).

- After any function that may return a backslash path (notably
  `tempfile.TemporaryDirectory()` on Windows), pass the result through
  `normalized_path()` BEFORE interpolating into a shell command string.
- `shlex.split()` (which `SubProcess` uses) eats backslashes as escapes - a raw
  Windows path embedded in a command string silently corrupts.
- For directory cleanup on Windows: `tempfile.TemporaryDirectory(prefix='...',
  ignore_cleanup_errors=True)` - git leaves read-only files in `.git/objects/`
  that trip `shutil.rmtree`.

## Subprocess: the two-tool rule

There are two primitives. They are NOT interchangeable.

- **`SubProcess.run(cmd, cwd=, io_func=, timeout=)`** - the project's standard
  wrapper. Uses `subprocess.Popen` + `pty.openpty()` on UNIX (child sees a real
  TTY for git's progress output) and plain `Popen` with pipes on Windows.
  Multi-thread safe. Has timeout. **Use this for everything by default.**
- **`subprocess.run(...)` directly** - only for the rare case where you need to
  suppress stderr entirely (`stderr=subprocess.DEVNULL`) and a timeout but don't
  want the live progress UI. The current example is the post-blob:none `git
  show HEAD:<file>` in `Git.fetch_self_version_from_remote` - its lazy fetch
  spews `remote: ...` chatter we don't want surfaced.

When deviating from `SubProcess.run`, document why in the function docstring.

**Never** use `os.system("cd <dir> && cmd")` - `SubProcess.run(cmd,
cwd=<dir>)` is the correct idiom. SubProcess uses `execve`, not a shell, so
`cd` and `&&` aren't valid.

**Never** use `os.forkpty()` directly anywhere in this codebase. Python 3.12
flags it as unsafe in multi-threaded programs, and mama runs heavy parallel
loads.

## Git commit style

- Single line, `<type>: <message>` prefix. Examples:
  `feature:`, `fix:`, `refactor:`, `release:`, `cleanup:`.
- No `Co-Authored-By` trailer in this repo (different from many others).
- Atomic commits: one logical change per commit. Bug fix + refactor → two
  commits, even when in one session.

## Artifactory + git status invariants

- **A 404 from artifactory for a git dep is NORMAL** (no prebuilt for current
  commit). It must NOT wipe the `git_status` file. Wiping the status causes the
  next `mama update` to read empty status → `check_status` → "SCM change
  detected" → spurious full rebuild. `check_status` already detects real
  url/tag/branch/commit changes via direct comparison.
- A 404 IS fatal for `is_pkg` deps (those URLs are mandatory).
- Shim probe (`try_load_artifactory_shim`) only runs when there's NO existing
  working tree (`not self.is_real_clone()`). For an already-cloned dep, the
  regular `fetch + reset` path is correct; running the probe in addition just
  re-clones into a tempdir and does nothing useful.

## SSH multiplex / parallel loading

- `mama update` auto-enables `parallel_load`. The `fetch_slot` semaphore caps
  concurrent git fetches at `parallel_max` (default 20). Independent of the
  worker thread count.
- The shim probe's `SubProcess.run` calls go through `fetch_slot` too - count
  the slot acquisitions per probe (one for the clone, possibly one for `git
  show`).
- `ensure_master_for_url` is idempotent and serialised per-host.

## Tests

- Test directories under `tests/test_<feature>/`. Each is a pytest package.
- Mock external IO (subprocess, urlopen, ftplib) heavily. Tests must not hit
  the network unless integration-flavored (`test_git_pin_change/`,
  `test_papa_deploy/`).
- When patching: `patch('mama.<module>.<name>')` - patch where it's looked up,
  not where it's defined.
- Always run the **full** suite (`python -m pytest tests/`) before committing.
  Total runtime ≈ 35 seconds.

## Mandatory final-stage review

**No feature, fix, or refactor is complete until the `mama-style-review`
skill has run against the pending changes and reported 0 issues.** This is
the last step of every task list, before the commit.

How to apply, every session:
1. After implementing the task and running tests, invoke the
   `/mama-style-review` skill (or spawn a sub-agent with that skill's prompt).
2. The skill reports findings as `<file>:<line> - <rule>: <fix>`.
3. Apply the fixes, re-run the review. Loop until `REVIEW PASSED - 0 issues`.
4. Only then commit.

The skill checks: 130-col limit, no 3+ line single expressions, no break
after `(`, one-liner `if`, no em-dashes, `warning()` instead of `Color.YELLOW`,
`normalized_path()` for paths, `SubProcess.run` over raw `subprocess.run`,
helper-reuse vs duplication (especially against `util.py` /
`utils/system.py`), and that any added behaviour has a test pinning it.

Trivial-looking diffs still need this; they routinely sneak in over-length
lines or em-dashes. No exceptions.
