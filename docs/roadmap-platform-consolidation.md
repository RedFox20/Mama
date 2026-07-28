# Roadmap: consolidate every platform into one `config.platform` object

**Status:** LANDED on branch `feature/platform-consolidation`. See section 12 for what shipped
and where it differs from the plan below.
**Audience:** an engineer or model picking this up cold. This document is self-contained.
**Baseline:** commit `e4040d7`, mama `0.13.06`. Test suite: 921 passed, 3 skipped, 44s.
**Verified on:** WSL2 Ubuntu, cmake 4.3.1, ninja 1.11.1.

Line numbers were accurate at `e4040d7`. Grep for the quoted code, never trust a number alone.

---

## 0. Start here

1. Read section 2 (the inventory). It is the evidence base for every later step.
2. Run `python -m pytest tests/ -q` and confirm 921 passed. That is the regression baseline.
3. Run the ground-truth capture in section 6.1. It writes the golden files the new tests use.
4. Start Phase 1 (section 5.1). It adds files only and changes no behavior.

**First concrete action:** create `mama/platforms/platform.py` with the `Platform` base class from
section 4.1.

---

## 1. Goal and evidence

### 1.1 The goal

Today a platform is 3 things at once:

- a **flag or object** on `BuildConfig` (`config.msvc` is a bool, `config.android` is an object),
- a **branch** in about 50 if/elif chains across 12 files,
- a **class** in `mama/platforms/`, but only for 8 of the 11 platforms.

The goal is one shape: every platform is a class, one instance lives at `config.platform`, and
every branch becomes a method call on it.

A second goal follows from the first. CMake is one build system of several. Platform facts and
CMake translation must not live in the same file. Today `mama/platforms/raspi.py` imports
`mama.cmake_configure`. That coupling blocks any second build system.

### 1.2 Evidence: the current state works

Every platform available on this box configures and builds correctly. This was measured, not
assumed. The probe project is a 1-file static library.

| Platform | Toolchain found | CMAKE_SYSTEM_PROCESSOR | Object file arch |
|---|---|---|---|
| `linux` | /usr/bin/gcc 14.3 | x86_64 | ELF 64-bit x86-64 |
| `android` | NDK 27.3.13750724, clang 18.0 | aarch64 | ELF 64-bit ARM aarch64 |
| `raspi` | /usr/bin/aarch64-linux-gnu-gcc 13.3 | aarch64 | ELF 64-bit ARM aarch64 |
| `raspi32` | /usr/bin/arm-linux-gnueabihf-gcc 13.3 | armv7-a | ELF 32-bit ARM EABI5 |
| `oclea` | /opt/oclea/1.0, gcc 11.5 | aarch64 | ELF 64-bit ARM aarch64 |
| `imx8mp` | /opt/imdt-imx-xwayland/5.0.4, gcc 13.3 | aarch64 | ELF 64-bit ARM aarch64 |
| `mips` | /usr/bin/mipsel-linux-gnu-gcc 12.3 | mipsel | ELF 32-bit MIPS32 rel2 |

`xilinx`, `ios`, `macos` and `msvc` have no toolchain on this box. Their tests must be mock-only.

**This table is the contract.** The refactor must reproduce it exactly.

### 1.3 Evidence: three real defects found while measuring

**D1. Android passes `CMAKE_MAKE_PROGRAM` twice.** Captured from a real configure:

```
... -DANDROID_USE_LEGACY_TOOLCHAIN_FILE=FALSE -DCMAKE_TOOLCHAIN_FILE="..." \
    -DCMAKE_MAKE_PROGRAM="/usr/bin/ninja" -DCMAKE_CXX_FLAGS="..." \
    -DCMAKE_MAKE_PROGRAM="/usr/bin/ninja" ...
```

`Android.get_cmake_build_opts` appends it at `mama/platforms/android.py:240`, and
`_default_options` appends it again at `mama/cmake_configure.py:660`. Only android does this,
because only android calls `_make_program` from inside its own option builder.

**D2. `add_platform_options(windows=...)` raises `TypeError`.** `README.md:408` documents
`self.add_platform_options(linux='LINUX_OPT=ON', windows='WIN_OPT=ON')`. The signature at
`mama/build_target.py:808` is `(self, msvc=None, linux=None, macos=None, ios=None, android=None)`.
Its two siblings at `:744` and `:759` use `windows=`. The README is right and the code is wrong.

**D3. Six platforms cannot be selected by any `add_platform_*` API.** `select()` at
`mama/build_target.py:818` covers msvc, linux, macos, ios and android only. `raspi`, `mips`,
`oclea`, `xilinx`, `imx8mp` and `yocto_linux` are documented as detection properties at
`README.md:381`, but no `add_platform_options` keyword reaches them.

### 1.4 Evidence: one latent risk

Yocto platforms emit **no** `CMAKE_SYSTEM_NAME` and no `CMAKE_SYSTEM_PROCESSOR` when a toolchain
file is set. `GenericYocto.get_cmake_build_opts` returns early at
`mama/platforms/generic_yocto.py:207`. Both SDKs on this box happen to set both variables inside
their own toolchain file, so the build is correct today.

Android does the opposite. It emits `cross_system_opts` **and** a toolchain file. The docstring on
`cross_system_opts` (`mama/cmake_configure.py:62`) records why: a seeded compiler cache makes cmake
skip system determination, and the processor then falls back to the host.

A Yocto SDK whose toolchain file omits `CMAKE_SYSTEM_PROCESSOR` reproduces the exact android bug,
silently. The consolidation removes the asymmetry.

---

## 2. Inventory: every platform-specific site

50 sites in 12 files. Grouped by file. This is the work list.

### 2.1 `mama/build_config.py` (1359 lines, 261 platform references)

| # | Lines | What | Chain size |
|---|---|---|---|
| 1 | 4-11 | 8 platform imports | - |
| 2 | 122-132 | 11 platform fields, mixed bool and object | - |
| 3 | 273-284 | 12 CLI arg branches, plus a `raspi32` arch side effect | 12 |
| 4 | 331-336 | `android-NN` and `ndk-NN` arg branches | 2 |
| 5 | 341-342 | `install-raspi` and `install-raspi32` args | 2 |
| 6 | 359-397 | `set_platform()`: a 10-slot bool array, a `get_new_value` closure, a yocto alias fixup | 10 |
| 7 | 400-403 | `is_platform_set()` | 8 |
| 8 | 406-445 | `check_platform()`: default arch per platform, then arch validation per platform | 6 + 4 |
| 9 | 448-482 | `get_distro_info()` | 8 |
| 10 | 496-505 | `name()` | 8 |
| 11 | 521-539 | 19 `build_dir_*()` accessor methods | 19 |
| 12 | 568-593 | `_platform_build_dir_name()` | 8 |
| 13 | 637-658 | `prefer_clang` / `prefer_gcc`, guarded by `not self.linux or self.raspi` | 2 |
| 14 | 748-790 | `get_preferred_compiler_paths()` | 6 |
| 15 | 802-819 | `compiler_version()` | 4 |
| 16 | 835-877 | `set_clang_tidy_path()`, android-special in 3 places | 3 |
| 17 | 922-990 | 6 `set_*_toolchain()` wrappers. 4 of them have identical bodies | 6 |
| 18 | 930-938 | `init_platform_toolchain()` | 4 |
| 19 | 1173-1316 | `install_clang` / `install_gcc` / `install_msbuild` / `install_ndk` / `install_raspi` and the dispatch | 5 |
| 20 | 1319-1325 | `libname()` and `libext()` | 2 |

`set_oclea_toolchain`, `set_imx8mp_toolchain`, `set_xilinx_toolchain` and `set_yocto_toolchain`
have byte-identical bodies. Only the docstring differs. That is 4 methods and 50 lines for 1
behavior.

### 2.2 `mama/cmake_configure.py` (715 lines)

| # | Lines | What | Chain size |
|---|---|---|---|
| 21 | 485-497 | `_generator()` | 9 |
| 22 | 500-505 | `_make_program()` | 3 |
| 23 | 511-527 | `_platform_opts()`, already half consolidated by `_CROSS_PLATFORMS` | 2 |
| 24 | 554-588 | `_default_options()` cxx flags. android, raspi, yocto and mips delegate. linux, macos, ios and msvc are inline | 6 |
| 25 | 643-644 | `get_ldflags_with_defaults`, a hook only yocto has | 1 |
| 26 | 660-661 | trailing `_make_program`, the source of defect D1 | - |
| 27 | 665-672 | `inject_env()`. android delegates. ios and macos are inline | 3 |
| 28 | 690-698 | `_mp_flags()` | 4 |
| 29 | 701-714 | `_buildsys_flags()` | 4 |

Site 23 is the model the rest should follow. It already dispatches through a tuple of platform
attribute names instead of an if/elif chain.

### 2.3 `mama/build_target.py` (1836 lines)

| # | Lines | What |
|---|---|---|
| 30 | 108-110, 118-130 | `_update_platform_aliases()` copies 11 fields off config. Called twice per target |
| 31 | 191-192 | `.exe` suffix for host tool binaries |
| 32 | 808-825 | `add_platform_options` and `select`. Defects D2 and D3 |
| 33 | 854-872 | `_get_cxx_std` / `_set_cxx_std`, `/std` against `-std` |
| 34 | 882-886 | `enable_cxx20`: mips, raspi and yocto get `c++2a` |
| 35 | 744, 759 | `add_platform_cxx_flags` and `add_platform_ld_flags`, same 5-platform limit |

### 2.4 `mama/dependency_chain.py` (1136 lines)

| # | Lines | What |
|---|---|---|
| 36 | 22-41 | `_get_exported_libs()`: allowed library extensions per platform |
| 37 | 265-296 | `_find_matching_platform_config()` for `.vscode/c_cpp_properties.json` |
| 38 | 396-525 | `_save_mama_cmake()`: a 130-line CMake if/elseif over every platform and build dir |
| 39 | 920-948 | buildstats: msvc uses vcperf, clang uses `-ftime-trace` |
| 40 | 995-1020 | `_toolchain_name()` and `_platform_name()` for the build banner |

Site 38 is the largest single block. It is a Python f-string that emits CMake, and it repeats every
`build_dir_*` name and every platform guard a second time.

### 2.5 Remaining files

| # | File and lines | What |
|---|---|---|
| 41 | `main.py:142-166` | `open_project()`: sln, xcodeproj or VSCode |
| 42 | `main.py:227` | coverage report is not supported on msvc |
| 43 | `package.py:19-21` | shared library extensions |
| 44 | `package.py:196-222` | `find_syslib()`: frameworks on apple, `.so` search on linux |
| 45 | `package.py:262-268` | `_reset_syslib_name()` strips `lib` and `.so` or `.a` |
| 46 | `utils/gnu_project.py:76-77` | GNU `--host` triple for mips and yocto |
| 47 | `utils/gdb.py:28-30` | "cannot run tests" list of 7 platforms |
| 48 | `utils/gdb.py:41-50` | debugger choice: none, lldb or gdb |
| 49 | `utils/run.py:20-22` | add or strip `.exe` |
| 50 | `artifactory.py:62-71` | archive name from distro, compiler version, arch |

### 2.6 Inconsistent names across the 8 existing platform classes

The same idea has a different name in every class. A base class cannot exist until these merge.

| Idea | Android | GenericYocto | Raspi | Mips | Ios |
|---|---|---|---|---|---|
| compiler dir | `bin()` | `bin()` | `compilers` field | - | - |
| compiler prefix | - | `gcc_prefix()` | `compiler_prefix()` | `compiler_prefix()` | - |
| include dirs | - | `includes()` | `get_includes()` | `includes()` | - |
| sysroot | - | `sysroot()` | `get_sysroot()` | - | - |
| build dir | - | `build_dir` field | `build_dir()` method | - | - |
| name | - | `name` field | - | `name` field | - |
| arch list | - | - | `supported_arches` | `supported_arches` | - |
| discovery | `init_ndk_path()` | `init_default()` | `init_default()` | `init_default()` | - |
| version | - | `distro_version` | - | `toolchain_major/minor` | - |
| C++ flags | `get_cxx_flags` | `get_cxx_flags` | `get_cxx_flags` | `get_cxx_flags` | inline |
| linker flags | - | `get_ldflags_with_defaults` | - | - | - |
| env | `inject_env` | - | - | - | inline |

`build_dir` is a field on one class and a method on another. `_platform_build_dir_name` therefore
reads `self.yocto_linux.build_dir` at line 576 and `self.raspi.build_dir()` at line 590.

---

## 3. What "consolidated" means

Four rules. Every later phase serves one of them.

1. **One instance.** `config.platform` holds the active platform. Nothing else stores platform state.
2. **One vocabulary.** Every platform answers the same method names. Section 4.1 fixes them.
3. **No platform branches outside `mama/platforms/`.** A branch becomes a method call.
4. **CMake lives in one place.** Platform classes describe a toolchain. A separate layer renders
   that description into cmake options.

Rule 4 is the reason for the `Toolchain` dataclass in section 4.2. Without it, rule 3 just moves
the cmake strings into the platform files, and the second build system stays blocked.

---

## 4. Target design

### 4.1 `mama/platforms/platform.py`

```python
class Platform:
    """One target platform. Facts only, no build-system knowledge."""

    # --- identity, set by the subclass ---
    name = ''                    # 'linux', 'android', 'imx8mp'. Also the CLI arg and archive tag
    cli_aliases = ()             # extra CLI args, eg ('windows', 'msvc')
    system_name = 'Linux'        # CMAKE_SYSTEM_NAME equivalent
    is_cross = False
    is_host_runnable = True      # can this box run the produced binaries?
    default_arch = 'x64'
    supported_arches = ('x64',)
    cmake_define = ''            # 'RASPI' emits RASPI=TRUE. '' emits nothing
    compile_defines = {}         # {'OCLEA': '1', 'YOCTO_LINUX': '1'}

    def __init__(self, config): self.config = config

    # --- toolchain discovery, overridden by cross platforms ---
    def init_toolchain(self, toolchain_dir=None, toolchain_file=None): pass
    def toolchain(self) -> Toolchain: ...      # lazy, cached. See 4.2

    # --- build dir and versioning ---
    def build_dir_name(self) -> str            # 'raspi32', 'linux', 'android'
    def distro_version(self) -> tuple          # ('linux', 24, 4)
    def compiler_version_tag(self) -> str      # 'gcc14.3' for the archive name

    # --- flags ---
    def get_cxx_flags(self, add_flag)
    def get_ld_flags(self, add_ld_flag)
    def inject_env(self)

    # --- tool and product naming ---
    def exe_suffix(self) -> str                # '.exe' or ''
    def lib_extensions(self) -> tuple          # ('.a', '.so') or ('.lib',)
    def gnu_host_triple(self) -> str           # for utils/gnu_project.py
    def debugger(self) -> str                  # 'gdb', 'lldb' or '' for none
    def open_command(self, dep) -> str         # for main.open_project

    # --- generated CMake support, see phase 5 ---
    def mama_cmake_guard(self) -> str          # 'RASPI', 'ANDROID OR ANDROID_NDK', 'WIN32'
```

Every method has a working default in the base. A platform overrides only what differs. `Linux`
overrides 3 methods. `Android` overrides 9.

11 subclasses: `Windows`, `Linux`, `Macos`, `Ios`, `Android`, `Raspi`, `Mips`, `GenericYocto`
(abstract), `Oclea`, `Xilinx`, `Imx8mp`.

### 4.2 `mama/platforms/toolchain.py`

The neutral description a build system renders. No cmake vocabulary in the field names, with one
deliberate exception.

```python
@dataclass
class Toolchain:
    system_name: str = 'Linux'      # Linux | Android | Darwin | Windows
    system_processor: str = ''      # aarch64 | armv7-a | x86_64 | mipsel
    cc: str = ''
    cxx: str = ''
    version: str = ''
    tool_prefix: str = ''           # prefix for ar, ranlib, strip, readelf
    sysroot: str = ''
    include_paths: tuple = ()
    toolchain_file: str = ''
    find_root_program: str = ''     # NEVER | ONLY | ''
    find_root_libs: str = ''
    build_with_install_rpath: bool = False
    extra_opts: tuple = ()          # escape hatch. See below
```

`extra_opts` is the escape hatch for options that only cmake understands and only one platform
needs. Android is the only real user, for `ANDROID_ABI`, `ANDROID_STL` and `ANDROID_NDK`. Keeping
those as raw strings is honest. Inventing neutral names for NDK-specific cmake variables would be
worse.

### 4.3 `mama/buildsys/` (new package)

```
mama/buildsys/__init__.py
mama/buildsys/cmake/__init__.py
mama/buildsys/cmake/options.py    # Toolchain -> ['-DCMAKE_SYSTEM_NAME=...', ...]
mama/buildsys/cmake/generator.py  # generator, make program, -j flags
mama/buildsys/cmake/configure.py  # today's cmake_configure.py, minus platform branches
mama/buildsys/cmake/mamacmake.py  # today's _save_mama_cmake, generated from the registry
mama/buildsys/msbuild.py          # today's mama/msbuild.py
```

`mama/cmake_configure.py` stays as a re-export shim so no import breaks in one step.

**The dependency rule:** `mama/platforms/*` must never import `mama/buildsys/*`. A CI test enforces
it, in the same style as the existing `test_every_toolchain_file_option_goes_through_the_helper`
test at `tests/test_configure_flags/test_configure_flags.py:34`.

### 4.4 `mama/platforms/registry.py`

```python
PLATFORMS = (Windows, Linux, Macos, Ios, Android, Raspi, Mips, Oclea, Xilinx, Imx8mp)

def platform_for_arg(arg: str) -> type | None    # 'raspi32' -> Raspi
def host_platform() -> type                      # from System.windows / linux / macos
```

`parse_args` replaces 12 branches with one registry lookup. `raspi32` maps to `(Raspi, 'arm')`, so
the arch side effect stays declarative.

### 4.5 Back-compat: `config.msvc` and friends stay

`self.linux`, `self.android`, `self.raspi` and 8 more are documented public mamafile API at
`README.md:381`. They must keep working. They become properties:

```python
@property
def linux(self) -> bool: return isinstance(self.platform, Linux)

@property
def android(self): return self.platform if isinstance(self.platform, Android) else None

@property
def yocto_linux(self): return self.platform if isinstance(self.platform, GenericYocto) else None
```

The bools stay bools and the object accessors stay objects. Every existing `if config.msvc:` and
every `config.android.android_api` keeps working unchanged.

`BuildTarget._update_platform_aliases` becomes properties that forward to `config`, so the
double call at `build_target.py:112` and `:116` disappears with it.

This property layer is what makes the whole refactor safe. It is not a temporary shim, it is the
public API.

---

## 5. Phases

Each phase lands alone and leaves the tree green. Do not batch them.

### Phase 1: the base class and the registry (adds only)

**Estimated 4 hours.**

1. Add `mama/platforms/platform.py` with the `Platform` base from 4.1.
2. Add `mama/platforms/toolchain.py` with the `Toolchain` dataclass from 4.2.
3. Add `mama/platforms/registry.py` with `PLATFORMS`, `platform_for_arg` and `host_platform`.
4. Add `tests/test_platforms/test_registry.py`. Pin every CLI arg to its class and arch.

Nothing imports the new files yet. The suite must still report 921 passed.

**Done when:** `platform_for_arg('raspi32')` returns `(Raspi, 'arm')` and the suite is green.

### Phase 2: migrate the 8 existing platform classes

**Estimated 6 hours.**

Order: `Ios`, `Mips`, `Raspi`, `GenericYocto` (then its 3 subclasses come free), `Android`.
Smallest first, so the base class shape is proven before android stresses it.

Per class:

1. Inherit `Platform`. Set the identity fields.
2. Rename to the section 4.1 vocabulary. Keep the old name as a 1-line alias where a mamafile could
   call it. `config.android.android_ndk()` and `config.android.android_api` are used by
   `dependency_chain._platform_name`, so they must survive.
3. Move the toolchain facts into a `Toolchain` the class builds once and caches.
4. Keep `get_cmake_build_opts` in place for now. It becomes a thin wrapper over
   `cmake.options.from_toolchain(self.toolchain())` plus `extra_opts`.
5. Add the golden test for that platform (section 6.2). It must pass before the next class starts.

**Fix D1 here.** Delete the `_get_make` call inside `Android.get_cmake_build_opts`
(`mama/platforms/android.py:240`). `_default_options` already appends it at
`cmake_configure.py:660`. Pin it with a test asserting `CMAKE_MAKE_PROGRAM` appears once.

**Fix the yocto asymmetry here.** `GenericYocto.get_cmake_build_opts` must emit
`cross_system_opts` even when a toolchain file is set, exactly as android does. Pin it with a test.

**Done when:** all 8 classes inherit `Platform`, the golden tests pass, and the suite is green.

### Phase 3: `config.platform` replaces the 11 fields

**Estimated 6 hours.** This is the highest-risk phase.

1. Add `Windows`, `Linux` and `Macos` classes. Move `_generator`, `_mp_flags`, `_buildsys_flags`,
   `libname`, `libext`, `open_command` and `debugger` branches into them.
2. Add `self.platform` to `BuildConfig.__init__`. Add the 11 back-compat properties from 4.5.
3. Replace `set_platform()` (inventory #6) with a registry assignment. Delete the bool array.
4. Collapse the chains, in this order. Each is a separate commit.
   1. `name()`, `_platform_build_dir_name()`, `is_platform_set()` (#7, #10, #12)
   2. `check_platform()` default arch and arch validation (#8)
   3. `get_distro_info()` and `compiler_version()` (#9, #15)
   4. `get_preferred_compiler_paths()` (#14). Reads `platform.toolchain().cc`
   5. `init_platform_toolchain()` and the 6 `set_*_toolchain` wrappers (#17, #18)
   6. `set_clang_tidy_path()` (#16)
   7. `parse_args` platform args (#3, #4, #5)
   8. `run_convenient_installs` (#19)
5. Delete the 19 `build_dir_*()` methods (#11). Keep them as a `BUILD_DIRS` dict for
   `_save_mama_cmake`, which is the only remaining consumer of the full set.
6. Replace `BuildTarget._update_platform_aliases` (#30) with forwarding properties.

**Fix D2 and D3 here.** Rewrite `select()` (#32) to take `**kwargs` keyed by platform name, so all
11 platforms work and `windows=` matches its two siblings. Keep `msvc=` as an accepted alias so no
existing mamafile breaks.

**Done when:** `grep -n "config\.\(msvc\|linux\|android\|raspi\|mips\|yocto_linux\)" mama/` returns
hits only in `build_config.py` properties and the `platforms/` package.

### Phase 4: split the cmake layer out

**Estimated 5 hours.**

1. Create `mama/buildsys/` per 4.3. Move files, keep `mama/cmake_configure.py` as a re-export shim.
2. Write `cmake/options.py:from_toolchain(tc) -> list`. It emits `CMAKE_SYSTEM_NAME`,
   `CMAKE_SYSTEM_PROCESSOR`, `CMAKE_SYSROOT`, the 4 `CMAKE_FIND_ROOT_PATH_MODE_*`, the binutils
   from `tool_prefix`, and the toolchain file through `use_toolchain_file`.
3. Delete `get_cmake_build_opts` from every platform class. `_platform_opts` (#23) calls
   `from_toolchain(config.platform.toolchain()) + platform.extra_opts`.
4. Remove `from mama.cmake_configure import ...` from every file under `mama/platforms/`.
5. Add `tests/test_platforms/test_layering.py`. Fail if any `mama/platforms/*.py` imports
   `mama.buildsys` or `mama.cmake_configure`.
6. Collapse `_default_options` cxx flags (#24), `inject_env` (#27) and `get_ldflags` (#25) to
   single `platform.` calls. Move the linux, macos and ios inline blocks into their classes.

**Done when:** `mama/platforms/` imports nothing from `mama/buildsys/`, and the golden tests still
pass byte for byte.

### Phase 5: generate `mama.cmake` from the registry

**Estimated 4 hours.**

`_save_mama_cmake` (#38) is 130 lines of CMake in an f-string. It restates every platform guard and
every build dir a second time. Drift between it and `_platform_build_dir_name` is silent.

1. Give `Platform` a `mama_cmake_guard()` and an arch-to-build-dir mapping.
2. Rewrite `_save_mama_cmake` as a loop over `PLATFORMS` plus a 25-line frame.
3. Add a test that the generated text still contains every `build_dir_*` value. That is the
   anti-drift check.

Order matters. `WIN32` and `APPLE` must come after `ANDROID`, and `UNIX` must come last, because
android is also UNIX. Encode the order in `PLATFORMS`, and pin it with a test.

**Done when:** the generated `mama.cmake` for a linux build is byte-identical to today's.

### Phase 6: the remaining 10 sites

**Estimated 4 hours.**

Sites #36, #37, #39, #40, #41, #42, #43, #44, #45, #46, #47, #48, #49, #50. Each is small.

- `#36` and `#43` become `platform.lib_extensions()`.
- `#44` and `#45` become `platform.syslib_search_paths()` and `platform.lib_prefix()`.
- `#46` becomes `platform.gnu_host_triple()`.
- `#47` becomes `platform.is_host_runnable`.
- `#48` becomes `platform.debugger()`.
- `#49` and `#31` become `platform.exe_suffix()`.
- `#41` becomes `platform.open_command(dep)`.

**Done when:** section 2's inventory has no remaining rows outside `mama/platforms/`.

---

## 6. Test plan

Every platform gets 3 test layers. Layer 1 and 2 run everywhere. Layer 3 skips when the toolchain
is absent.

### 6.1 Capture the ground truth first

Before Phase 1, run this once. It records what the refactor must reproduce.

```bash
D=$(mktemp -d); mkdir -p $D/src
cat > $D/CMakeLists.txt <<'EOF'
cmake_minimum_required(VERSION 3.15)
project(probe C CXX)
add_library(probe STATIC src/probe.cpp)
target_include_directories(probe PUBLIC src)
install(TARGETS probe ARCHIVE DESTINATION lib)
EOF
echo 'int probe_fn() { return 42; }' > $D/src/probe.cpp
printf 'import mama\nclass probe(mama.BuildTarget):\n    def settings(self):\n        self.enable_cxx17()\n' > $D/mamafile.py
cd $D
for p in linux android raspi raspi32 oclea imx8mp mips; do
  mama build $p verbose 2>&1 | grep -E "^cmake -G" > $OLDPWD/tests/test_platforms/expected/$p.txt
done
```

Then template the machine-specific paths. Replace the NDK version, the SDK roots and the source
dir with placeholders. The golden file records the **option set**, not the exact command string.

### 6.2 Layer 1: golden configure options, per platform (unit, mocked)

`tests/test_platforms/test_configure_options.py`. One parametrized test over all 11 platforms.

Each case:
1. Build a `BuildConfig` for that platform with the toolchain search mocked to a fake tree.
2. Build a `BuildTarget` through `testutils.make_configured_target`.
3. Call `cmake_configure._platform_opts(target)` and `_default_options(target)`.
4. Assert the option **set** equals the golden set. Order-insensitive, so a reorder is not a failure.

The fake tree comes from a shared `tests/testutils.py` helper, not from a per-file copy:

```python
def fake_toolchain_tree(tmp_path, platform_name) -> str:
    """Materialize the on-disk layout `platform_name` discovery expects. Returns the root."""
```

It covers 4 layouts: NDK (`toolchains/llvm/prebuilt/<host>/bin`), yocto (`sysroots/<sdk>` and
`sysroots/<sysroot>`), standalone cross (`bin/<triple>-gcc` and `<triple>/sysroot`) and distro
cross (`bin/<triple>-gcc` only).

**Per-platform assertions to pin:**

| Platform | Must assert |
|---|---|
| `linux` | `-march=native` on an x64 host. No `CMAKE_SYSTEM_NAME` |
| `linux` clang | `-stdlib=libc++`, and `libstdc++` after `use_gcc_stdlib_for_clang()` |
| `windows` | the VS generator id, `-A x64`, `/EHsc`, `/MP` |
| `macos` | `-G Xcode`, `-stdlib=libc++`, `MACOSX_DEPLOYMENT_TARGET` |
| `ios` | `IOS_PLATFORM=OS`, `CMAKE_OSX_ARCHITECTURES=arm64`, `-miphoneos-version-min` |
| `android` | `CMAKE_SYSTEM_NAME=Android`, `CMAKE_SYSTEM_PROCESSOR=aarch64`, `ANDROID_ABI=arm64-v8a`, the NDK toolchain file, and **exactly one** `CMAKE_MAKE_PROGRAM` (defect D1) |
| `android` armv7 | `ANDROID_ABI=armeabi-v7a`, `-march=armv7-a`, `-mfpu=neon` |
| `raspi` | `RASPI=TRUE`, `CMAKE_SYSTEM_PROCESSOR=aarch64`, `-march=armv8-a`, no `-mfpu` |
| `raspi32` | `CMAKE_SYSTEM_PROCESSOR=armv7-a`, `-march=armv7-a`, `-mfpu=neon-vfpv4` |
| `oclea` | `OCLEA=TRUE`, `-DOCLEA=1`, `-DYOCTO_LINUX=1`, `-mcpu=cortex-a53+crypto`, `-Wl,--as-needed`, and `CMAKE_SYSTEM_PROCESSOR=aarch64` (the section 1.4 fix) |
| `imx8mp` | `IMX8MP=TRUE`, `-DIMX8MP=1`, the poky toolchain file |
| `xilinx` | `XILINX=TRUE`, `-mcpu=cortex-a72.cortex-a53+crc`, `-mbranch-protection=standard` |
| `mips` | `MIPS=TRUE`, `CMAKE_SYSTEM_PROCESSOR=mipsel`, `FIND_ROOT_PATH_MODE_PROGRAM=ONLY` |

Note the last row. Mips uses `ONLY` where raspi and yocto use `NEVER`. That difference is real and
must survive the consolidation. Add a comment saying why, or the next refactor will "fix" it.

### 6.3 Layer 2: platform class contract (unit, no cmake)

`tests/test_platforms/test_contract.py`. One parametrized test over `PLATFORMS`.

Assert for every class:
1. `name` is non-empty and unique across the registry.
2. `default_arch` is in `supported_arches`.
3. Every `Platform` method is either inherited or returns the declared type.
4. `build_dir_name()` is unique per (platform, arch) pair.
5. The class imports nothing from `mama.buildsys` or `mama.cmake_configure`.

Rule 4 catches the class of bug where two platforms share a build dir and clobber each other.

### 6.4 Layer 3: real cmake configure (integration, skipped when absent)

`tests/test_platform_configure/test_real_configure.py`.

```python
@pytest.mark.parametrize('platform,probe_path', [
    ('linux',   None),
    ('android', os.getenv('ANDROID_NDK_HOME')),
    ('raspi',   '/usr/bin/aarch64-linux-gnu-gcc'),
    ('raspi32', '/usr/bin/arm-linux-gnueabihf-gcc'),
    ('mips',    '/usr/bin/mipsel-linux-gnu-gcc'),
    ('oclea',   '/opt/oclea/1.0'),
    ('imx8mp',  '/opt/imdt-imx-xwayland/5.0.4'),
])
def test_configure_and_build_produce_the_right_target_arch(platform, probe_path, tmp_path):
    if probe_path and not os.path.exists(probe_path): pytest.skip(f'no {platform} toolchain')
    ...
```

Each case writes the probe project into `tmp_path`, runs a real configure and build, then reads the
object file header. It asserts the ELF machine matches the table in section 1.2.

This is the test that literally answers "can it correctly run cmake configure steps".

Cost: about 12 seconds per platform on this box, so about 90 seconds total. Too slow for the
default suite. Mark it `@pytest.mark.slow` and add `-m "not slow"` to the default pytest args.
Run it explicitly before a release.

### 6.5 Test-code rules for this work

From `CLAUDE.md`. The new suite is 3 directories and about 12 files, so the rules matter.

1. `fake_toolchain_tree` and the golden loader live in `tests/testutils.py`. Never per file.
2. Use the `tmp_path` fixture. Never `tempfile.mkdtemp` with a try and finally.
3. Module docstring is 1 to 2 lines. The design rationale goes in the commit message.
4. No per-test docstring that restates the test name.
5. Comments say why. Explain the mips `ONLY` versus raspi `NEVER` choice. Do not explain the assert.

---

## 7. Duplication to remove

Ranked by lines removed. Land the top 3 even if the rest slips.

| Rank | What | Where | Lines saved |
|---|---|---|---|
| 1 | `_save_mama_cmake` CMake template, generated from the registry | `dependency_chain.py:396-525` | ~105 |
| 2 | The 19 `build_dir_*()` methods and the 8-branch `_platform_build_dir_name` | `build_config.py:521-593` | ~55 |
| 3 | 4 identical `set_*_toolchain` wrappers | `build_config.py:940-982` | ~40 |
| 4 | `set_platform()` bool array and `get_new_value` closure | `build_config.py:359-397` | ~35 |
| 5 | `Android.cc_path` and `cxx_path`, identical but for one character | `platforms/android.py:50-69` | ~15 |
| 6 | `BuildTarget._update_platform_aliases`, 11 fields copied twice | `build_target.py:118-130` | ~15 |
| 7 | `get_distro_info` and `compiler_version` chains | `build_config.py:448-482, 802-819` | ~30 |
| 8 | `_generator`, `_mp_flags`, `_buildsys_flags` chains | `cmake_configure.py:485-497, 690-714` | ~25 |

Total removable: about 320 lines. New code: about 250 lines across 11 classes plus the registry and
the `Toolchain` dataclass. Net is roughly break-even in line count, but the branch count drops from
about 50 chains to about 5.

**Do not measure this refactor by line count.** Measure it by "how many files must a new platform
touch". Today the answer is 9 files and about 20 sites. After Phase 6 it is 1 file plus 1 registry
line.

### 7.1 The new-platform checklist, as it will read after Phase 6

```python
# mama/platforms/newboard.py
class NewBoard(GenericYocto):
    name = 'newboard'
    system_name = 'Linux'
    default_arch = 'arm64'
    supported_arches = ('arm64',)
    cmake_define = 'NEWBOARD'
    compile_defines = {'NEWBOARD': '1', 'YOCTO_LINUX': '1'}

    def init_toolchain(self, toolchain_dir=None, toolchain_file=None):
        self._yocto_init(toolchain_dir, toolchain_file, paths=['/opt/newboard/1.0'], ...)

    def get_cxx_flags(self, add_flag):
        add_flag('-mcpu', 'cortex-a55')
        super().get_cxx_flags(add_flag)
```

Then add `NewBoard` to `PLATFORMS` in `registry.py`. That is the whole change.

---

## 8. Risks

| Risk | Why it matters | Mitigation |
|---|---|---|
| A mamafile in the wild reads `config.raspi` and expects a bool, not an object | `raspi` is a bool today at `build_config.py:127` but an object after Phase 3 | Keep the property returning the object, as `android` already does. `if self.raspi:` works either way. Grep downstream mamafiles before landing Phase 3 |
| The seed cache fingerprint changes and mass-invalidates warm build dirs | `_seed_id` hashes `_platform_opts` output at `cmake_configure.py:196` | Phase 2 and 4 must keep the option **set** identical. The golden tests are exactly this check. Any intended change ships as its own commit with a note |
| Yocto emitting `CMAKE_SYSTEM_NAME` breaks an SDK whose toolchain file conflicts | Section 1.4 fix adds options that were absent | Land it alone in Phase 2. Verify with the real oclea and imx8mp toolchains on this box before the rest of Phase 2 |
| `_save_mama_cmake` guard order changes and android matches `UNIX` first | Silently picks the wrong build dir | Pin the order with a test in Phase 5. Generate a `mama.cmake` for android and assert it selects `android`, not `linux` |
| The refactor is large and a bisect lands mid-way | A broken intermediate state costs a day | 6 phases, each green. Never batch. Section 5 lists the commit boundaries inside each phase |
| Windows and macOS paths cannot be tested here | 4 of 11 platforms have no toolchain on this box | Layer 1 and 2 are mock-only and cover them. Layer 3 skips. Run the full matrix in CI before a release |

---

## 9. Schedule

| Phase | Work | Hours |
|---|---|---|
| 0 | Capture ground truth (section 6.1) | 1 |
| 1 | Base class, `Toolchain`, registry, registry test | 4 |
| 2 | Migrate 8 classes, fix D1 and the yocto asymmetry, golden tests | 6 |
| 3 | `config.platform`, 3 new classes, collapse 8 chains, fix D2 and D3 | 6 |
| 4 | `mama/buildsys/` split, layering test | 5 |
| 5 | Generate `mama.cmake` | 4 |
| 6 | Remaining 14 sites | 4 |
| 7 | `/mama-style-review` loop to 0 issues | 3 |
| | **Total** | **33** |

About 4 to 5 working days. Phases 1 and 2 alone (11 hours) already deliver the base class, the
golden tests and 2 real bug fixes. That is a good stopping point if the rest slips.

---

## 10. Done criteria

1. `config.platform` is the only platform state on `BuildConfig`.
2. `grep -rn "config\.\(msvc\|linux\|macos\|ios\|android\|raspi\|mips\|yocto_linux\)"` over `mama/`
   hits only `build_config.py` properties.
3. No file under `mama/platforms/` imports `mama.buildsys` or `mama.cmake_configure`.
4. All 11 platforms have a golden configure test that passes.
5. The 7 platforms with a real toolchain pass the layer 3 integration test and produce the object
   arch in the section 1.2 table.
6. `python -m pytest tests/` reports at least 921 passed, with no test deleted without a reason in
   the commit message.
7. `/mama-style-review` reports `REVIEW PASSED - 0 issues`.
8. Defects D1, D2 and D3 are fixed and each has a test.
9. Adding a platform touches 1 new file and 1 registry line.

---

## 11. Out of scope

These came up during the analysis. They are real, but they are not this work.

1. **A second build system.** Phase 4 unblocks it. It does not add one.
2. **`config.name()` against `platform.name`.** They mean the same thing. Merging them touches the
   artifactory archive name, which changes package identity. That needs its own migration plan.
3. **The `System` class and the `os_windows` aliases.** Host detection is a separate axis from
   target platform. Leave it alone.
4. **`find_compiler_root` and the compiler suffix search.** Compiler selection is orthogonal to
   platform selection. Touch it only where Phase 3 step 4.4 requires.
5. **The msvc toolset and Visual Studio discovery block** (`build_config.py:1005-1170`, 165 lines).
   It belongs in a `Windows` platform class. Move it in a follow-up, once Phase 3 proves the shape.

---

## 12. What actually landed

Branch `feature/platform-consolidation`, 8 commits, all green at every step.

### 12.1 Differences from the plan

| Plan | Reality | Why |
|---|---|---|
| Phases 1, 2 and 3 land separately | Landed as one commit | The 11 mamafile flags had to become properties in the same change that installed `config.platform`, or the tree is broken in between |
| `cmake_configure.py` stays put with a re-export shim | Moved to `mama/buildsys/cmake/configure.py` | A shim breaks every `patch('mama.cmake_configure.X')` target, so the honest move was cheaper than the shim |
| `select()` takes positional args | Takes `**platforms` | Positional could not carry 11 platforms, and it is what made `windows=` disagree with `msvc=` |
| A Yocto board keeps an `init_toolchain` method | A Yocto board is pure class attributes | Every Yocto SDK has the same layout, so the method was the same code three times |

### 12.2 Extra defects found and fixed while building it

Beyond D1, D2 and D3 from section 1.3:

1. **The four MIPS arches shared one build dir.** `mips`, `mipsel`, `mips64` and `mips64el` all
   built into `packages/<target>/mips`, so a big-endian build overwrote a little-endian one in
   place. `mipsel` keeps the bare dir, so existing packages do not churn. The others got their own.
   Found by the new `test_no_two_platform_and_arch_pairs_share_a_build_dir`.
2. **MIPS named no cross binutils.** It sets `CMAKE_FIND_ROOT_PATH_MODE_PROGRAM=ONLY` and no find
   root, so cmake's search for `ar` and `ranlib` was not restricted to the toolchain.
3. **`package.get_lib_basename` was defined twice.** The second definition shadowed the first, so
   the tuple-aware version was dead and a tuple lib name would raise `AttributeError`.
4. **`gnu_project.py` called `warning()` without importing it.** A deploy that copied zero files
   raised `NameError` instead of printing the warning.
5. **A Yocto board read its toolchain file before SDK discovery ran.** It worked only because
   `init_platform_toolchain()` happens to run first on the normal path.

### 12.3 Measured result

| Metric | Before | After |
|---|---|---|
| Platform classes | 8 (of 11 platforms) | 11 |
| `if/elif` chains over platforms | ~50 | 0 outside `mama/platforms/` |
| `mama/build_config.py` | 1359 lines | 1193 lines |
| A Yocto board | 32 to 47 lines of code | 15 to 17 lines of declarations |
| Files to touch to add a platform | 9 | 2 (the new file, plus one registry line) |
| Tests | 921 | 1173, plus 8 real-toolchain tests |

`mama/platforms/` grew (3 new platforms, a base class, a registry, a Toolchain type) while every
consumer shrank. The branch count is the metric that mattered, and it went to zero.

### 12.4 Verified on real toolchains

`python -m pytest tests/test_platform_configure -m slow` runs a real cmake configure and build and
reads the ELF header of the object each platform produced. On this machine: linux, android, raspi,
raspi32, mips, oclea and imx8mp all pass. xilinx skips (no PetaLinux SDK installed).

### 12.5 Still open

- Section 11 items 2 to 5 are unchanged and still out of scope.
- The MSVC and Visual Studio discovery block (`build_config.py`, ~165 lines) still belongs in the
  `Windows` platform class. Phase 3 proved the shape, so this is now a mechanical follow-up.
- `Android.inject_env()` sets `CMAKE_MAKE_PROGRAM` in the environment. That is the variable name the
  NDK's own tooling reads, so it stayed, but it is the last cmake name left in `mama/platforms/`.
