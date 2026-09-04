# How mama handles platforms

A platform is what mama builds FOR: `linux`, `android`, `imx8mp`, and eight more. For the
mamafile-facing API, see the README.

## Three layers

```
mama/platforms/     what a platform IS       facts only, no build-system vocabulary
mama/buildsys/      how to drive a build     renders those facts into -D options
everything else     asks config.platform     no chain over platform names
```

**A platform never formats a build-system option.** `mama/buildsys/cmake/options.py` is the only
module that turns a platform fact into a `-D` flag. That is what lets a second build system read the
same facts and write its own. `tests/test_platforms/test_layering.py` fails the build if a platform
imports `mama.buildsys` or names a `CMAKE_` variable.

**No consumer chains over platform names.** What a consumer needs, it declares on `Platform` and
reads back. A handful of single `config.msvc` checks remain, because MSVC differs as a compiler
family and a flag syntax (`/EHsc`, `:` instead of `=`), not as a target.

## The pieces

| File | What it holds |
|---|---|
| `mama/platforms/platform.py` | `Platform`, the base class. Every declaration a platform can make, each with a working default |
| `mama/platforms/toolchain.py` | `Toolchain`, the neutral description of a resolved compiler set |
| `mama/platforms/registry.py` | `PLATFORMS`, the ordered list, and the CLI arg lookup |
| `mama/platforms/generic_yocto.py` | `GenericYocto`, the shared base for a board that ships a Yocto SDK |
| `mama/platforms/gnu_cross.py` | `GnuCross`, the shared base for a board built with a plain `<triple>-gcc` |
| `mama/platforms/<name>.py` | one platform. Mostly class attributes |
| `mama/buildsys/cmake/options.py` | `Toolchain` to cmake `-D` options. The ONLY place that mapping exists |
| `mama/buildsys/cmake/configure.py` | the configure and build driver, the compiler-seed cache |
| `mama/buildsys/cmake/mamacmake.py` | generates the consumer-facing `mama.cmake` from the registry |
| `mama/buildsys/msbuild.py` | the other build system |

## One instance, one vocabulary

`config.platform` holds the single active platform. `set_platform_class()` installs it and derives
the mamafile-facing flags (`config.linux`, `config.android`, ...) from it, so those keep working
while nothing else stores platform state. `BuildTarget` forwards the same names as properties.

Discovery is lazy and cached: `platform.toolchain()` runs `init_default()` once, then builds the
`Toolchain` once. A root mamafile's `settings()` runs before that, so `config.set_toolchain()` always
wins over the default SDK search paths.

## The two shared bases

Most boards are one of two shapes, and each has a base that already knows it. A board on either one
declares data, never behavior.

- **`GenericYocto`** - the board ships a Yocto SDK: `sysroots/<host sdk>/` with the cross compilers,
  `sysroots/<target>/` with the target libraries, and a cmake toolchain file of its own. `Oclea`,
  `Xilinx` and `Imx8mp` are this.
- **`GnuCross`** - the board ships nothing, and the build uses a plain GNU cross toolchain:
  `<triple>-gcc` in some `bin/` dir, the target headers inside the compiler, and no sysroot. The
  distro cross package (`apt install g++-aarch64-linux-gnu`) is this shape, and so is a standalone
  toolchain tarball. `Raspi` and `Aarch64` are this.

`Aarch64` (`aarch64`) is the fallback for any 64-bit ARM Linux board whose vendor never published an
SDK: the build uses the distro cross toolchain and the project links its binaries statically.

`aarch64` used to be an alias of the arm64 **arch**. It names this platform now, and
`mama build android aarch64` raises instead of switching platforms - see below. `arm64` is the
spelling for an arch pin, and always was the documented one.

Its `platform_define` is `AARCH64_LINUX`, not `AARCH64`: the generated `mama.cmake` matches the arm64
arch with a regex whose own text contains `(AARCH64)`, and CPU-detection code everywhere sets a plain
`AARCH64`. A guard by that name would fire for a consumer that set it for its own reasons.

## Two platform args is an error

`select_platform_arg` raises when a second CLI arg names a *different* platform. Last-one-wins was
silent, and what it produced was a working build for the wrong target: `mama build android aarch64`
cross-compiled for linux and said nothing about it.

Two args for the *same* platform stay fine, because that is exactly what an arch alias is:
`raspi raspi32` is Raspi pinned to arm, and `windows msvc` is one platform under two names.
`_FORMER_ARCH_ARGS` in `build_config.py` maps an arg that used to pin an arch to the word that pins
it now, so `aarch64` after another platform names `arm64` in the error instead of a generic message.

## Adding a platform

Two edits. Write `mama/platforms/newboard.py`:

```python
class NewBoard(GenericYocto):       # or GnuCross, or Platform for something neither shape fits
    name = 'newboard'               # the CLI arg, the build dir and the archive tag
    default_arch = 'arm64'
    supported_arches = ('arm64',)
    host_triple = 'aarch64-newboard-linux'
    search_paths = ('/opt/newboard/1.0',)
    compiler_name = 'usr/bin/aarch64-newboard-linux/aarch64-newboard-linux-gcc'
    sdk_name = 'x86_64-newboardsdk-linux'
    sysroot_name = 'cortexa55-newboard-linux'
    default_toolchain = 'aarch64_newboard_toolchain.cmake'
    cpu_flags = {'-mcpu': 'cortex-a55'}
```

A `GnuCross` board declares a different vocabulary - what it is, not where its SDK lives:

```python
class NewBoard(GnuCross):
    name = 'newboard'               # no `-`: the artifactory archive name splits its fields on that
    display_name = 'New Board'      # what a message calls it. Falls back to `name`
    default_arch = 'arm64'
    platform_define = 'NEWBOARD'
    triples = {'arm64': 'aarch64-linux-gnu'}   # supported_arches is derived from this
    marches = {'arm64': 'armv8-a'}
    search_envs = ('NEWBOARD_HOME',)           # read before the default paths, so a user override wins
    linux_paths = ('/opt/newboard', '/usr')    # <path>/bin/<triple>-gcc
```

Then wire the name in. It is more than the registry, and every one of these is load-bearing:

| Edit | Why |
|---|---|
| `PLATFORMS` in `registry.py` | selects the platform from a CLI arg, and drives every parametrized test |
| `_GUARDS` in `buildsys/cmake/mamacmake.py` | a platform with no guard is invisible to a consumer's CMakeLists |
| `_PLATFORM_FLAGS` and the two `self.<name>` lines in `build_config.py` | `config.<name>` for a mamafile |
| the `_flag` tuple at the bottom of `build_target.py` | `self.<name>` on a BuildTarget. A missing name is an AttributeError |
| the arg list in `main.py` | `mama help` |
| `README.md` platform list | the only place a user finds the env var |

`tests/test_platforms/` covers the rest: the registry, contract, layering and generated-cmake tests
are all parametrized over `PLATFORMS`. Add the board to `tests/test_platforms/conftest.py` too - a
platform with no fake toolchain tree resolves against whatever the dev box happens to have installed.

A platform that needs real behavior overrides a method instead of declaring an attribute. `Android`
overrides the most (NDK discovery, ABI naming, its own make program). `Linux` overrides only a few.

## The instruction set

A platform declares its `-march` one of three ways, or declares none. A fixed value goes in
`cpu_flags`, which the base class reads. A value that follows the arch overrides `default_march()`, as
`Linux`, `Macos` and `Android` do. A `GnuCross` board just fills in its `marches` table, which the
base reads for it. `Xilinx`, `Mips` and `Ios` declare none of the three and get no `-march`.

None of them emits the flag. `Platform.get_cxx_flags` calls `march()`, which takes the project pin from
`config.set_target_march()` when there is one, and `default_march()` otherwise. So exactly one `-march`
reaches the compiler, and a platform never has to know about the pin. A platform whose compiler has no
`-march` declares `supports_march = False` and gets none.

## What the host can run

`Platform.also_runs` maps a host arch to what that host runs BESIDES its own arch. The base rule is
that an x64 host also runs an x86 build. `Macos` adds x64 on Apple silicon, through Rosetta, and it
probes for that install. `Windows` adds both x86 and x64 on an arm64 host, and it
checks the Windows version, because the x64 emulator arrived in Windows 11. `build_names.is_host_build` reads it to decide whether a build is
already a host build, so `build_host_binary` does not build the same tool twice.

## Invariants the tests enforce

1. No `if/elif` chain over platform names outside `mama/platforms/`. A consumer that needs
   per-platform behavior declares it on `Platform`.
2. No platform imports `mama.buildsys` or writes a `CMAKE_` name. `Toolchain.extra_opts` is the
   documented escape hatch, used by iOS (Xcode SDK selection has no neutral form) and by Android
   (variables only the NDK's own toolchain file understands).
3. Every `(platform, arch)` pair gets its own build dir. A shared one means two builds clobber each
   other's cache and libraries.
4. Every platform answers the same method names with the same parameters.
5. A cross platform always emits its target system name AND processor, even with a toolchain file. A
   seeded compiler cache makes cmake skip system determination, so a missing processor silently falls
   back to the host's.
6. `mama.cmake` guards are tested in registry order. android is also UNIX and iOS is also APPLE, so
   the specific guard has to come first.

## Verifying a real toolchain

`tests/test_platforms/` is all mocked and runs in under a second. To prove a cross build really
cross-compiles, run the integration suite, which configures and builds for real and reads the ELF
header of the object each platform produced:

```
python -m pytest tests/test_platform_configure -m slow
```

A platform whose SDK is not installed on the machine skips. A release runs `python -m pytest tests/ -m slow`,
which adds every other slow test to this one.
