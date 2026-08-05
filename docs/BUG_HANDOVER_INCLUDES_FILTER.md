# Mama bug handover: an artifactory package deploy ignores `includes_filter`

Reproduced with mama 0.13.12. Reporter: KrattGCS build (`/home/jorma/krattcam/krattgcs`).

## Summary

`package()` sets the include deploy filter through `export_include(includes_filter=...)`.
Mama skips `package()` when the target loads from artifactory. The papa deploy that follows
then uses the default filter `['.h','.hpp','.hxx','.hh']`. Every other header suffix the
recipe asked for is dropped from the deployed tree and from the uploaded archive.

The published package is therefore incomplete, and every consumer of that package fails to
compile.

## Symptom

KrattGCS CI, linux job, `mama build linux deps_only`:

```
+ build J0   protobuf      [linux] art  11.6s  cfg   0.0s  bld   0.0s
x build J5   krattlinkrpc  [linux] loc  0.05s  cfg   0.2s  bld   0.2s

packages/protobuf/linux/include/absl/numeric/int128.h:1194:10: fatal error:
    absl/numeric/int128_have_intrinsic.inc: No such file or directory
 1194 | #include "absl/numeric/int128_have_intrinsic.inc"  // IWYU pragma: export
compilation terminated.
```

`protobuf` resolves from artifactory (`art`). The archive
`protobuf-ubuntu-24-gcc13.3-x64-release-34.0.zip` holds 623 `.h` files and 0 `.inc` files.
Abseil splits `int128.h` across two `.inc` files, so the header tree is unusable.

## The consumer recipe is correct

`mamadeps/protobuf.py` already declares the suffix:

```python
def package(self):
    self.export_include("include", build_dir=True,
                        includes_filter=[".h", ".hpp", ".hxx", ".hh", ".inc"])
```

The same recipe produces a correct package when mama builds the target from source. The
locally built android package holds 30 `.inc` files in both the build include dir and the
deploy dir. Only the artifactory path loses them.

## Root cause, in call order

1. `mama/build_target.py:113` sets the default `self.include_glob_filter =
   ['.h','.hpp','.hxx','.hh']`.
2. `mama/build_target.py:506` `export_include()` overwrites that field from
   `includes_filter`. Only `package()` calls it.
3. `mama/build_target.py:1683` guards the `package()` hook:
   `if self.dep.should_rebuild or not self.dep.from_artifactory:`.
   A target loaded from artifactory falls through to
   `mama/build_target.py:1718`, which only records
   `packaging_result = f'artifactory-cache {...}'`. The hook never runs, so
   `include_glob_filter` keeps the default value.
4. `mama/build_target.py:1735` `_execute_deploy_tasks()` still runs on a `deploy` or an
   `upload`. It calls `self.deploy()` at line 1753 and `papa_upload_to()` at line 1758.
5. `mama/papa_deploy.py:102` reads `suffixes = tuple(target.include_glob_filter)`, which is
   the default. `_append_includes()` copies only those four suffixes into the deploy tree.
6. `papa_upload_to()` zips that deploy tree. The archive ships without the `.inc` files.

`papa.txt` records the export *list* (`I include`, `L lib/...`, `A bin/protoc`). It does not
record the export *rules*. So `artifactory_load_target()` restores the paths and loses the
filter.

## Proof

Run against a `protobuf` package already fetched from artifactory:

1. Add a marker header and a marker include to the fetched package:

```bash
D=packages/protobuf/linux/include/absl/numeric
echo "// marker" > $D/zz_marker.inc
echo "// marker" > $D/zz_marker.h
rm -rf packages/protobuf/linux/deploy
```

2. Re-deploy the target:

```bash
mama linux deploy protobuf
```

3. Read the deployed tree:

```
$ ls packages/protobuf/linux/deploy/protobuf/include/absl/numeric/
bits.h  int128.h  internal  zz_marker.h
```

`zz_marker.h` survives. `zz_marker.inc` disappears. The recipe asked for both.

`mama verbose linux deploy protobuf` names the branch that caused it:

```
- Package protobuf  (artifactory-cache protobuf-ubuntu-24-gcc13.3-x64-release-34.0)
```

That string comes from `build_target.py:1718`, the branch that skips `package()`.

## What raises the severity

`mama/papa_deploy.py:221` (commit `f287ba6`, released in 0.13.11) deletes the include tree
before the copy:

```python
remove_tree(f'{package_full_path}/include')
```

The intent is correct. A stale header must not ship. The effect on this bug is destructive.
Before 0.13.11 a re-deploy left the unpacked `.inc` files in place, because the copy only
added files. From 0.13.11 the deploy wipes the tree first and then re-copies with the wrong
filter. A re-deploy of an artifactory target now actively removes files the recipe exports.

## History

The guard dates back to `1ad6e6f` (2022-10-14),
"fix: set from_artifactory to avoid unnecessar dependencies() and package() calls".
The motivation was to avoid work that a fetched package does not need. `package()` also
asserts on build outputs, and a fetched target has no build dir, so the hook can raise.
The reporter reads this as a skip that solved one problem and introduced this one.

`mama/build_target.py:1660` already carries `_run_package_hook()`, which catches an exception
from `package()` and downgrades it to a warning during a `list` run. That is the shape a fix
can reuse.

## What the fix must satisfy

1. A deploy or an upload of a target loaded from artifactory honors every filter and every
   rule the recipe sets in `package()`.
2. `mama <platform> upload if_needed <target>` publishes an archive with the same file set
   that a from-source build publishes.
3. A `package()` that reads a build product does not break a run that built nothing. The
   fetched package must stay usable.
4. No consumer workaround. The reporter rejects a version bump on the artifact key, and
   rejects setting `include_glob_filter` from `settings()`. Both hide a mama defect in every
   mamafile.

## Design notes for the fix

The reporter's position: run `package()` again on the artifactory path, and fix whatever the
2022 commit was avoiding, instead of keeping the skip.

Points to weigh:

- `include_glob_filter` is not the only state `package()` sets. `target.includes_root`
  (from `as_includes_root`) drives `_include_deploy()` at `mama/papa_deploy.py:85`.
  `no_includes` and `no_libs` gate the default packaging at `build_target.py:1700`. All of
  them are absent on the artifactory path today.
- `_run_packaging()` already clears `exported_includes`, `exported_libs`, `exported_syslibs`
  and `exported_assets` before it re-runs the hook for a rebuild of an artifactory target
  (`build_target.py:1685`). The same reset path can serve the deploy and the upload case.
- A narrower option is to run `package()` only when `config.deploy` or `config.upload` is
  set. That covers the archive, and it leaves a plain build untouched.
- A third option is to record the filter in `papa.txt`, so a load restores it. This keeps
  the skip, but it needs a papa format change and it does not restore the other rules.

## Suspected second instance, not confirmed

The KrattGCS android build fails on a missing include from the `qcoro` package:

```
packages/qcoro/android/include/QCoro/qcorothread.h:10:10: fatal error:
    'qcoro/coroutine.h' file not found
```

The lowercase `qcoro/` include dir is absent from that fetched package. The linux `qcoro`
package holds both `QCoro/` and `qcoro/`. `_append_includes()` handles the case-variant pair
at `mama/papa_deploy.py:112`, and `target.includes_root` steers `_include_deploy()`. Both
depend on state that `package()` sets. This looks like the same defect through a different
field, but the reporter has not verified it.

## Suggested regression test

1. Build a small target whose `package()` calls `export_include()` with a non-default
   `includes_filter`, for example `.inc`.
2. Deploy it, and assert the deploy tree holds the `.inc` file.
3. Load the same target as an artifactory package, with `from_artifactory` set.
4. Deploy it again, and assert the deploy tree still holds the `.inc` file.

Step 4 fails today. `tests/test_papa_include_records.py` is the closest existing suite.

## Reference

| Location | Role |
| --- | --- |
| `mama/build_target.py:113` | default `include_glob_filter`, no `.inc` |
| `mama/build_target.py:506` | `export_include()` sets the filter, called only from `package()` |
| `mama/build_target.py:1660` | `_run_package_hook()`, the guarded hook call |
| `mama/build_target.py:1683` | the guard that skips `package()` for an artifactory target |
| `mama/build_target.py:1718` | the `artifactory-cache` branch, which sets no packaging state |
| `mama/build_target.py:1753` | `deploy()`, which runs for an artifactory target |
| `mama/papa_deploy.py:102` | the deploy reads `include_glob_filter` |
| `mama/papa_deploy.py:221` | `remove_tree()` of the include tree, added in 0.13.11 |
| `mamadeps/protobuf.py:31` | the KrattGCS recipe that needs `.inc` |
| `tools/cache_libraries.py:68` | CI upload, `mama <platform> upload if_needed <lib>` |
