# How mama handles platforms

A platform is what mama builds FOR: `linux`, `android`, `imx8mp`, and seven more. For the
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

## Adding a platform

Two edits. Write `mama/platforms/newboard.py`:

```python
class NewBoard(GenericYocto):       # or Platform, for a non-Yocto target
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

Then add `NewBoard` to `PLATFORMS` in `registry.py` and a guard to `_GUARDS` in `mamacmake.py`.
`tests/test_platforms/` covers it from there: the registry, contract, layering and generated-cmake
tests are all parametrized over `PLATFORMS`.

A platform that needs real behavior overrides a method instead of declaring an attribute. `Android`
overrides the most (NDK discovery, ABI naming, its own make program). `Linux` overrides only a few.

## The instruction set

A platform declares its `-march` one of two ways, or declares none. A fixed value goes in `cpu_flags`,
which the base class reads. A value that follows the arch overrides `default_march()`, as `Linux`,
`Macos`, `Raspi` and `Android` do. `Xilinx`, `Mips` and `Ios` declare neither and get no `-march`.

Neither one emits the flag. `Platform.get_cxx_flags` calls `march()`, which takes the project pin from
`config.set_target_march()` when there is one, and `default_march()` otherwise. So exactly one `-march`
reaches the compiler, and a platform never has to know about the pin. A platform whose compiler has no
`-march` declares `supports_march = False` and gets none.

## What the host can run

`Platform.host_runs` maps a host arch to the arches that host can run. The base rule is that an x64
host also runs an x86 build. `Macos` adds x64 on Apple silicon, through Rosetta, and `Windows` adds
both x86 and x64 on an arm64 host. `build_names.is_host_build` reads it to decide whether a build is
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

A platform whose SDK is not installed on the machine skips.
