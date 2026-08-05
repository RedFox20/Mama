# Handover: the load phase has no live display, and the first attempt swallowed output

Attempted against mama 0.13.12 plus the unreleased 0.13.13 work. Reporter: KrattGCS build
(`/home/jorma/krattcam/krattgcs`). The attempt is REVERTED. Nothing of it is committed.

## What the reporter asked for

`mama list` and every other untargeted command take the classic path, whose load phase prints one
plain line per dependency:

```
  - Target nlohmannjson     SHIM FETCHED nlohmannjson-ubuntu-24-gcc13.3-x64-release-75fb64a
  - Target nlohmannjson
  - Target reflect_cpp      SHIM FETCHED reflect_cpp-ubuntu-24-gcc13.3-x64-release-v0.25.0
  - Target reflect_cpp
```

The reporter reads that as a serial dump, and asked for the shape the build phase already draws:

1. One live display line per dependency during the load, the way `execute_unified` draws it.
2. A summary of every fetch, and only THEN the package listing.
3. The package listing names the archive its exports came from.

Point 3 shipped separately in `82aaca9`. Points 1 and 2 are open.

Note on wording: the reporter first called the load "serial". It is not. `load_dependency_chain`
sets `parallel_load = True` unless the run passes `serial`. The complaint is the output shape.

## The attempt

`load_dependency_chain(root, with_display=False)` gained a display, and `mamabuild` passed
`with_display=True` for the untargeted classic load only.

- The root loads OUTSIDE the display, so its `settings()` output reaches the terminal. A
  mis-picked toolchain must never hide inside a live region. `execute_unified` loads the root the
  same way, for the same reason.
- Every other dep ran inside `_run_phase(display, dep, 'load', body, None, final=True)`.
- `with_display` stays False for `revive_deferred_target_deps` and `reload_deferred_deps`. A nested
  reload would otherwise open a second live region inside the first.
- The build log had to survive two display sessions in one run. That part was correct and shipped
  on its own as `e811de2`: the run owns the log, a display never closes it.

## Outcome 1: a shared dep drew one line per parent

```
+ artifactory   ReCpp   [linux] art 0.7s
+ artifactory   ReCpp   [linux] art 0.7s  art 0.7s
+ artifactory   ReCpp   [linux] art 0.7s  art 0.7s  art 0.7s
... eleven lines, one per parent of ReCpp
```

Cause: `load_dependency` tests `dep.already_loaded` before it loads. Under the parallel load every
parent of a shared dep reaches that test first. No thread has set the flag yet, so all of them call
the body. `dep.load()` is serialized by its own lock and runs once. Each caller still opened a
display task, and `_run_phase` adds to `dep.phase_times[kind]` every time.

The attempt fixed this with an atomic claim. One thread claims the display line through a set and a
lock, and the rest wait inside `load()`. Re-measured on the reporter tree, 20 lines and 0
duplicates. **This fix is sound and worth keeping.**

## Outcome 2: 13 of 33 deps printed nothing at all

This is why the attempt was reverted.

```
deps=33  lines=20
not shown: compression geo krattgcs krattlink krattlinkrpc krattlinkservice
           logging mapproviders protobuf px4gpsdrivers typedsettings utilities windproviders
```

Nine of those are local modules. The rest are krattlink, protobuf and px4gpsdrivers.

Those deps DO print on the classic path. Under `noart` each one reports
`- Target utilities NO ARTIFACTORY PKG [LOAD noart override]`. With the display they report
nothing, so the display loses output that the plain path shows. That is a regression, and it is
worse than the shape the reporter wanted to improve.

Measured both piped and inside a real tty (`script -qec`), with the ANSI codes stripped BEFORE the
match and carriage returns split into lines. An earlier count of "13 of 33" and a later "0 of 33"
were both measurement errors from matching before stripping. Trust the numbers above.

## Where to look

`system.capture_to(sink, display, tid, build_slot)` routes the `console()` calls of the running
thread into that display task. The load body of the missing deps writes its lines there. The task
then never commits a summary line, so both the captured lines and the summary disappear.

Start at `_run_phase` in `mama/dependency_chain.py` and at `finish_task` in
`mama/utils/build_display.py`. Ask what a task commits when it has captured output but no
subprocess. Then ask how a load that took milliseconds differs from one that fetched an archive.

## Reproduce

```
cd /home/jorma/krattcam/krattgcs
mama list noart 2>&1 | sed 's/\x1b\[[0-9;]*[A-Za-z]//g; s/\r/\n/g' | grep -E '^\s*\+ ' | awk '{print $3}' | sort -u
```

Compare that list against the names in the `ALL Dependency List` line of the same run. Every name
must appear exactly once.

## Acceptance

1. Every dep in the graph draws exactly one load line, shared deps included.
2. No line that the classic path prints disappears.
3. The package listing still prints below the live region, as plain lines.
4. One build log per run holds both phases.
