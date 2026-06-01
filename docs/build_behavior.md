# mama build vs update: dependency-loading behaviour

Reference spec for what the dependency loader must and must NOT do under each
top-level command. Captures invariants that the build-target/shim plumbing has
to honour. Update this file when the contract changes; the code is the truth,
but this document is the intent.

## The two commands

**`mama build`** - build using whatever is currently checked out / cached.
For deps with a valid shim: no network, no re-unzip, no ls-remote.

**`mama update`** - refresh all deps then build. ls-remote per dep,
artifactory cache zip may be re-fetched, shim build_dirs are re-extracted.

**`mama build noart`** - no artifactory fetches. ls-remote is still allowed
on shimmed deps so upstream-advanced shims can be detected and dropped;
that triggers a clone+build-from-source fallback.

`mama build` is the hot path. It runs many times per developer per day. It
MUST be cheap.

## Dependency states

For a non-root git dep, the loader sees one of these on-disk states:

1. **Valid shim, papa.txt present** - the dep has been previously satisfied
   from artifactory. `mama_shim` marker exists, `papa.txt` exists in the
   build_dir, build products are extracted. This is the steady state of
   shimmed deps.
2. **Stale shim** - marker exists but the upstream commit advanced (only
   detectable via ls-remote).
3. **Real clone** - a `.git` directory exists; the dep is built from source.
4. **Empty** - first-time load. Nothing yet on disk.

## What `mama build` MUST do per state

### State 1: valid shim, papa.txt present (the common case)

- Load papa.txt from the existing build_dir.
- Construct the BuildTarget, attach the exports/deps.
- Set `did_check_artifactory = True` so downstream code skips probes.
- Done. No network, no zip, no extraction.

Printed line:
```
  - Target opencv           OK (shim cached)
```
or similar. Specifically **not** `SHIM FETCHED` (misleading - nothing was
fetched), and **not** `Artifactory cache /path/to.zip` (no zip was touched).

### State 2: stale shim (rare, but must not silently miss it)

- Without `update`: trust the shim. Do NOT auto-detect staleness on every
  `mama build`. The user opted into a fast build; the cost of an ls-remote
  per shim across N deps is exactly the wasted work this doc exists to
  prevent.
- With `update`: ls-remote, detect mismatch, drop marker, fall through to
  the regular clone+probe path.
- Edge case: under `noart`, ls-remote IS still performed (it's cheap, and
  noart already trades the artifactory fetch for a build-from-source if
  upstream advanced).

### State 3: real clone

- Regular `dependency_checkout` path runs (`fetch + reset` only with
  `update`; with `build` only verify HEAD).
- Post-clone artifactory load only if `should_load_artifactory()` says so
  (a previous papa.txt exists, first-time build, or `is_pkg`).

### State 4: empty

- Probe artifactory via ls-remote (no clone yet). On hit: extract the zip
  to build_dir, write the shim marker, write papa.txt. This is the path
  that legitimately prints `SHIM FETCHED`.
- On miss: clone the repo, then re-probe artifactory after the mamafile is
  parsed (catches target.version-pinned deps).

## What `mama update` MUST do per state

The opposite intent: refresh everything.

- State 1: ls-remote to check staleness. If unchanged, may still re-extract
  if the cache zip has been re-downloaded (covers package-format upgrades).
- State 2-4: same as build, but cached package files are re-fetched from
  artifactory rather than reused.

The `target.config.update and target.is_current_target()` guard in
`artifactory_fetch_and_reconfigure` is the chokepoint that bypasses the
local cache check. It is correct for `update`; it must NOT be reached
under plain `build`.

## Implementation

`BuildDependency._try_artifactory_shim` honours `try_load_cached_shim` when a
shim marker exists and `config.update` is False. The cached path's
`check_staleness` parameter gates the ls-remote probe: True under noart, False
under plain build. Update bypasses the cached path entirely so the regular
probe re-extracts.

Tests pinning the behaviour:
- `tests/test_build_shim_cache/` - plain `mama build` cached fast path
- `tests/test_noart_shim_cache/` - noart cached-with-staleness-check path
