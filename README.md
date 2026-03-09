# Mama Build Tool
Mama - A modular C++ build tool even your mama can use

The main goal of this project is to provide extremely convenient in-source builds
for cross platform projects. Building is as simple as `mama build windows` - no ceremony~!

CMake projects with trivial configurations and no dependencies can be handled
automatically by Mama. This makes header-only libraries or stand-alone C libraries
extremely easy to link.

Adding projects with already configured `mamafile.py` is trivial and allows you to manage
large scale projects in a modular way. Dependencies are added and configured through mamafiles.

Each mama build target exports CMake `${ProjectName}_INCLUDES` and `${ProjectName}_LIBS`. All exports
are gathered in correct linker order inside `MAMA_INCLUDES` and `MAMA_LIBS`. This ensures the least
amount of friction for developers - everything just works.

There is no central package repository, all packages are pulled and updated from public or
private git repositories. Package versioning is done through git tags or branches.

Custom build systems are also supported. For additional documentation explore: [build_target.py](mama/build_target.py)


## Who is this FOR?
Anyone who develops cross-platform C++ libraries or applications which
target any combination of [Windows, Linux, macOS, iOS, Android, Raspberry, Oclea, Xilinx, MIPS, i.MX8MP].
And anyone who is not satisfied with system-wide dependencies and linker
bugs caused by incompatible system-wide libraries on Linux.

If you require an easy to use, reproducible project/namespace scoped package+build system, this is for you.
Your builds will not rely on hard to setup system packages, all you need to do is type `mama build`.

### Supported platforms ###
- Windows (64-bit x86_64, 32-bit x86, 64-bit arm64, 32-bit armv7) default is latest MSVC
- Linux (Ubuntu) (64-bit x86_64, 32-bit x86, 64-bit arm64) both GCC and Clang
- MacOS (64-bit x86_64, 64-bit arm64) via config.macos_version
- iOS (64-bit arm64) via config.ios_version
- Android (64-bit arm64, 32-bit armv7) via env ANDROID_NDK_HOME or ANDROID_HOME
- Raspberry (32-bit armv7) via env RASPI_HOME
- Oclea (64-bit arm64) via config.set_oclea_toolchain() or env OCLEA_HOME
- i.MX8M Plus (64-bit arm64 NXP i.MX8M Plus) via config.set_imx8mp_toolchain() or env IMX8MP_SDK_HOME
- MIPS (mips, mipsel, mips64, mips64el) via config.set_mips_toolchain()
- Xilinx (64-bit arm64 Zynq UltraScale+ MPSoC) via config.set_xilinx_toolchain() or env XILINX_HOME

## Who is this NOT for?
Single platform projects with platform specific build configuration and system wide dependency management
such as Linux exclusive G++ projects using apt-get libraries or iOS-only apps using cocoapods.


## Artifactory
Provides a mechanism to upload pre-built packages to a private artifactory server through `mama upload mypackage`. These packages will be automatically used if a git:package commit hash matches.


## Setup For Users
1. Get Python 3.10+ and PIP
2. `$ pip install mama --upgrade`
3. `$ cd yourproject`
4. `$ mama init` which creates a `mamafile.py` and patches your CMakeLists.txt
5. (optional) Manual setup: Create your own `mamafile.py` (examples below) and add this to your CMakeLists.txt:
```cmake
include(mama.cmake)
include_directories(${MAMA_INCLUDES})
target_link_libraries(YourProject PRIVATE ${MAMA_LIBS})
```
6. `$ mama build` and enjoy!
7. `$ mama open` to open your project in an IDE / VSCode


## Command examples
```
  mama init                      Initialize a new project. Tries to create mamafile.py and CMakeLists.txt
  mama build                     Update and build main project only. This only clones, but does not update!
  mama build x86 opencv          Cross compile build target opencv to x86 architecture
  mama build android             Cross compile to arm64 android NDK
  mama build android-26 arm      Cross compile to armv7 android NDK API level 26
  mama update                    Update all dependencies by doing git pull and build.
  mama clean                     Cleans main project only.
  mama clean x86 opencv          Cleans opencv for x86 architecture.
  mama clean all                 Cleans EVERYTHING in the dependency chain for current arch.
  mama rebuild                   Cleans, update and build main project only.
  mama build dep1                Update and build dep1 only.
  mama update dep1               Update and build the specified target.
  mama serve android             Update, build and deploy for Android.
  mama deploy                    Runs PAPA deploy stage.
  mama wipe dep1                 Wipe target dependency completely and clone again.
  mama upload dep1               Deploys and uploads dependency to Artifactory server.
  mama list                      List all mama dependencies on this project.
  mama dirty dep1                Mark a target for rebuild even if it was up to date.
  mama version                   Show the mama package version.
  mama test                      Run tests on main project.
  mama test=arg                  Run tests on main project with an argument.
  mama test="arg1 arg2"          Run tests on main project with multiple arguments.
  mama test dep1                 Run tests on target dependency project.
  mama dep1 start=dbtool         Call target project mamafile start() with args [`dbtool`].
```
Call `mama help` for more usage information.

### Build flags
```
  release                        (default) Build with CMake RelWithDebInfo configuration.
  debug                          Build with CMake Debug configuration.
  clang                          Prefer Clang compiler on Linux.
  gcc                            Prefer GCC compiler on Linux.
  x86 | x64 | arm | arm64       Select target architecture.
  arch=<arch>                    Override cross-compiling architecture explicitly.
  jobs=N                         Limit maximum parallel compilations.
  with_tests                     Forces -DENABLE_TESTS=ON and -DBUILD_TESTS=ON.
  fortran                        Enable automatic Fortran compiler detection.
  flags="-Wextra -O3"            Pass additional compiler flags.
  silent                         Greatly reduces output verbosity.
  verbose                        Greatly increases output verbosity.
  parallel                       Load dependencies in parallel.
  unshallow                      Allow unshallowing shallow git clones.
```

### Artifactory flags
```
  if_needed                      Only upload if package does not already exist on server.
  art                            Always fetch packages from artifactory; failure will throw.
  noart                          Temporarily ignore artifactory package fetching.
```

### Sanitizer and coverage flags
```
  sanitize=address               Enable -fsanitize=<type> for GCC/Clang.
  asan                           Shorthand for sanitize=address.
  lsan                           Shorthand for sanitize=leak.
  tsan                           Shorthand for sanitize=thread.
  ubsan                          Shorthand for sanitize=undefined.
  coverage                       Build with GCC --coverage option.
  coverage-report[=src_root]     Generate coverage report using gcovr.
```

### Install utilities
```
  install-clang6                 Install Clang 6 for Linux.
  install-clang11                Install Clang 11 for Linux.
  install-msbuild                Install MSBuild for Linux.
  install-ndk                    Install Android NDK for Linux or Windows.
```

## Mamafile Reference

### Overridable methods

Mamafile classes extend `mama.BuildTarget` and can override these methods:

| Method | Description |
|--------|-------------|
| `dependencies(self)` | Add git, local, or artifactory dependencies |
| `settings(self)` | Define settings (called first, after clone) |
| `configure(self)` | Pre-build CMake configuration options |
| `build(self)` | Override the default `cmake_build()` behavior |
| `package(self)` | Post-build: define exported includes, libs, assets |
| `install(self)` | Override the default `cmake_install()` step |
| `deploy(self)` | Custom deployment logic |
| `clean(self)` | Custom pre-clean steps |
| `test(self, args)` | Test runner invoked by `mama test` |
| `start(self, args)` | Custom entrypoint invoked by `mama start=<arg>` |
| `init(self)` | Initialization after mamafile is loaded |

### Class attributes

| Attribute | Default | Description |
|-----------|---------|-------------|
| `workspace` | `None` | Local workspace folder for build intermediates |
| `global_workspace` | `None` | System-wide workspace folder name |
| `cmake_build_type` | `'RelWithDebInfo'` | CMake build type (or `'Debug'` with `debug` flag) |
| `cmake_lists_path` | `'CMakeLists.txt'` | Path to the CMakeLists.txt relative to source |
| `cmake_command` | `'cmake'` | CMake executable path |
| `enable_exceptions` | `True` | Enable C++ exceptions |
| `enable_ninja_build` | `True` (if found) | Use Ninja generator when available |
| `enable_unix_make` | `False` | Force Unix Makefiles generator |
| `enable_cxx_build` | `True` | Enable C++ compiler |
| `enable_multiprocess_build` | `True` | Enable parallel compilation |
| `clean_intermediate_files` | `False` | Clean intermediate build files after build |
| `version` | `None` | Custom version string for packaging |

### Platform detection properties

Use these boolean properties in mamafiles for platform-conditional logic:
`self.windows`, `self.linux`, `self.macos`, `self.ios`, `self.android`,
`self.raspi`, `self.oclea`, `self.xilinx`, `self.imx8mp`, `self.mips`, `self.yocto_linux`

Host OS detection: `self.os_windows`, `self.os_linux`, `self.os_macos`

### C++ standard selection (overrides CMakeLists.txt)
```py
self.enable_cxx11()   # or enable_cxx14(), enable_cxx17(), enable_cxx20(), enable_cxx23(), enable_cxx26()
```

### Compiler flags
```py
self.add_cxx_flags('-Wall', '-Wextra')               # C++ only flags
self.add_c_flags('-std=c11')                         # C only flags
self.add_cl_flags('-fPIC')                           # Both C and C++ flags
self.add_ld_flags('-lm')                             # Linker flags
self.add_platform_cxx_flags(linux='-fPIC', windows='/W4')  # Per-platform C++ flags
self.add_platform_ld_flags(linux='-pthread')               # Per-platform linker flags
```

### CMake configuration
```py
self.add_cmake_options('BUILD_SHARED_LIBS=ON', 'OPTION=VALUE')
self.add_platform_options(linux='LINUX_OPT=ON', windows='WIN_OPT=ON')
self.enable_from_env('CUDA')  # enable CMake option CUDA=ON if CUDA=1 env var is set
```

### Package exports
```py
self.export_includes(['include'])                    # Export include dirs from source dir
self.export_include('include', build_dir=True)       # Export single include dir from build dir
self.export_libs('.', ['.lib', '.a'])                 # Find and export libs matching patterns
self.export_lib('lib/mylib.a')                       # Export a specific library file
self.export_syslib('GL')                             # Export a system library
self.export_asset('data/model.bin', category='models')  # Export asset files
self.export_assets('data/', ['.bin', '.dat'])         # Export multiple assets by pattern
self.no_export_includes()                            # Suppress automatic include exports
self.no_export_libs()                                # Suppress automatic lib exports
```

### Execution utilities
```py
self.run('make install', src_dir=True)               # Run a shell command
self.run_program('/usr/local/bin', './tool --flag')  # Run program in a specific directory
self.gdb('bin/MyTests')                              # Run with GDB/LLDB debugger
self.gtest('bin/MyTests', args, gdb=True)            # Run GTest executable with XML reports
self.gnu_project('zlib', '1.2.13', url='...')        # Build a GNU autotools project
self.ms_build('project.vcxproj', properties={})      # Build with MSBuild (for C#/.NET apps)
self.cmake_build()                                   # Build . with CMake (default build() implementation)
```

### File and download utilities
```py
self.copy(src, dst, filter=None)                     # Copy files
self.copy_built_file('Release/mylib.dll', 'bin/')    # Copy a build artifact
self.download_file('https://...', 'local_dir/')      # Download a file
self.download_and_unzip('https://.../sdk.zip', 'sdk/')  # Download and extract
self.source_dir('subpath')                           # Get absolute source directory path
self.build_dir('subpath')                            # Get absolute build directory path
```

### Compiler and build system control
```py
self.prefer_gcc()                                    # Prefer GCC on Linux (DEFAULT)
self.prefer_clang()                                  # Prefer Clang on Linux
self.visibility_hidden()                             # Set -fvisibility=hidden
self.disable_ninja_build()                           # Force CMake default generator instead of Ninja (default)
self.disable_install()                               # Skip cmake install step
self.enable_fortran()                                # Enable Fortran compiler (for Fortran accelerated libraries)
self.disable_cxx_compiler()                          # Disable C++ (C-only project)
self.nothing_to_build()                              # Mark target as header-only/no-build
```

### Deployment
```py
self.papa_deploy('path/to/package')                  # Deploy package for upload
self.default_deploy()                                # Deploy with default settings
```

## Mamafile examples

Project `AlphaGL/mamafile.py`
```py
import mama
class AlphaGL(mama.BuildTarget):
    # where to build intermediates
    workspace = 'packages' # for system-wide workspace, use: global_workspace = 'mycompany'

    # grab dependencies straight from git repositories
    # if the projects are trivial, then no extra configuration is needed
    def dependencies(self):
        # set artifactory package server for prebuilt packages
        # the credentials can be configured by env vars for CI, call `mama help`
        self.set_artifactory_ftp('artifacts.myftp.com', auth='store')
        # add packages
        self.add_git('ReCpp',   'https://github.com/RedFox20/ReCpp.git', branch='master')
        self.add_git('libpng',  'https://github.com/LuaDist/libpng.git')
        self.add_git('libjpeg', 'https://github.com/LuaDist/libjpeg.git')
        self.add_git('glfw',    'https://github.com/glfw/glfw.git')

        # add local packages from existing directory root:
        self.add_local('utils', 'libs/utils')

        # add a prebuilt package, use `mama upload myproject` to generate these:
        self.add_artifactory_pkg('opencv', version='df76b66')
        if self.linux: # or do it conditionally for linux only:
            self.add_artifactory_pkg('opencv', fullname='opencv-linux-x64-release-df76b66')

    # optional: customize package exports if repository doesn't have `include` or `src`
    def package(self):
        self.export_libs('.', ['.lib', '.a']) # export any .lib or .a from build folder
        self.export_includes(['AGL']) # export AGL as include from source folder
        # platform specific system library exports:
        if self.ios:   self.export_syslib('-framework OpenGLES')
        if self.macos: self.export_syslib('-framework OpenGL')
        if self.linux: self.export_syslib('GL')

    def test(self, args):
        self.gdb(f'bin/AlphaGLTests {args}')
```

If a dependency is non-trivial (it has dependencies and configuration),
you can simply place a target mamafile at: `mama/{DependencyName}.py`

Example dependency config `AlphaGL/mama/libpng.py`
```py
import mama
class libpng_static(mama.BuildTarget):
    def dependencies(self):
        # custom mamafile can be passed explicitly:
        self.add_git('zlib', 'https://github.com/madler/zlib.git', mamafile='zlib.py')

    def configure(self):
        zinclude, zlibrary = self.get_target_products('zlib')
        self.add_cmake_options(f'ZLIB_INCLUDE_DIR={zinclude}')
        self.add_cmake_options(f'ZLIB_LIBRARY={zlibrary}')
        self.add_cmake_options('BUILD_SHARED_LIB=NO', 'PNG_TESTS=NO')

    def package(self):
        # libpng builds its stuff into `{build}/lib`
        self.export_libs('lib', ['.lib', '.a'])
        # export installed include path from build dir
        self.export_include('include', build_dir=True)
```

## Example output from Mama Build
```
$ mama build
========= Mama Build Tool ==========
  - Target FaceOne            BUILD [root target]
  - Target dlib               OK
  - Target CppGuid            OK
  - Target opencv             OK
  - Target ReCpp              OK
  - Target NanoMesh           OK
  - Package ReCpp
    <I>  build/ReCpp/ReCpp
    [L]  build/ReCpp/windows/RelWithDebInfo/ReCpp.lib
  - Package opencv
    <I>  build/opencv/windows/include
    [L]  build/opencv/windows/lib/Release/opencv_xphoto342.lib
    [L]  build/opencv/windows/lib/Release/opencv_features2d342.lib
    [L]  build/opencv/windows/lib/Release/opencv_imgcodecs342.lib
    [L]  build/opencv/windows/lib/Release/opencv_imgproc342.lib
    [L]  build/opencv/windows/lib/Release/opencv_core342.lib
    [L]  build/opencv/windows/3rdparty/lib/Release/libjpeg-turbo.lib
    [L]  build/opencv/windows/3rdparty/lib/Release/libpng.lib
    [L]  build/opencv/windows/3rdparty/lib/Release/zlib.lib
  - Package dlib
    <I>  build/dlib/windows/include
    [L]  build/dlib/windows/lib/dlib19.15.99_relwithdebinfo_64bit_msvc1914.lib
  - Package NanoMesh
    <I>  build/NanoMesh/NanoMesh
    [L]  build/NanoMesh/windows/RelWithDebInfo/NanoMesh.lib
  - Package CppGuid
    <I>  build/CppGuid/CppGuid/include
    [L]  build/CppGuid/windows/RelWithDebInfo/CppGuid.lib
  - Package FaceOne
    <I>  include
    [L]  bin/FaceOne.dll
    [L]  bin/FaceOne.lib
```
### Uploading packages ###
```python
    def dependencies(self):
        self.set_artifactory_ftp('ftp.myartifactory.com', auth='store')
        self.add_git('googletest', 'git@github.com:RedFox20/googletest.git')
```
```
$ mama upload googletest
========= Mama Build Tool ==========
  - Package googletest
    <I>  myworkspace/googletest/linux/include
    [L]  myworkspace/googletest/linux/lib/libgmock.a
    [L]  myworkspace/googletest/linux/lib/libgtest.a
  - PAPA Deploy /home/XXX/myworkspace/googletest/linux/deploy/googletest
    I (googletest)       include
    L (googletest)       libgmock.a
    L (googletest)       libgtest.a
  PAPA Deployed: 1 includes, 2 libs, 0 syslibs, 0 assets
  - PAPA Upload googletest-linux-x64-release-ebb36f3  770.6KB
    |==================================================>| 100 %
```
And then rebuilding with an artifactory package available
```
$ mama rebuild googletest
========= Mama Build Tool ==========
  - Target googletest         CLEAN  linux
  - Target googletest         BUILD [cleaned target]
    Artifactory fetch ftp.myartifactory.com/googletest-linux-x64-release-ebb36f3  770.6KB
    |<==================================================| 100 %
    Artifactory unzip googletest-linux-x64-release-ebb36f3
  - Package googletest
    <I>  myworkspace/googletest/linux/include
    [L]  myworkspace/googletest/linux/libgmock.a
    [L]  myworkspace/googletest/linux/libgtest.a
```


## Environment Variables

| Variable | Description |
|----------|-------------|
| `MAMA_ARTIFACTORY_USER` | Username for Artifactory server (CI usage) |
| `MAMA_ARTIFACTORY_PASS` | Password for Artifactory server (CI usage) |
| `NINJA` | Path to Ninja build executable (enables Ninja builds if Ninja is detected) |
| `ANDROID_HOME` | Path to Android SDK |
| `ANDROID_NDK_HOME` | Path to Android NDK |
| `ANDROID_NDK_ROOT` | Alternative Android NDK path |
| `ANDROID_NDK_LATEST_HOME` | Path to latest Android NDK |
| `RASPI_HOME` | Path to Raspberry Pi toolchain |
| `OCLEA_HOME` | Path to Oclea SDK |
| `IMX8MP_SDK_HOME` | Path to i.MX8M Plus SDK |
| `XILINX_HOME` | Path to Xilinx SDK |

## VSCode Integration

Mama automatically generates `compile_commands.json` (via `CMAKE_EXPORT_COMPILE_COMMANDS=ON`) and updates `.vscode/c_cpp_properties.json` with the correct `compileCommands` path for IntelliSense support.


## For Mama Contributors
We are open for any improvements and feedback via pull requests.

### Development Setup
The package `setuptools>=65.0,<77` is required, ensure the version is correct with `pip3 show setuptools`.

You can set up local development with `$ pip3 install -e . --no-cache-dir` but make sure you have latest setuptools (>=65.0,<77) and latest pip3 (>22.3). This command will fail with older toolkits.

### Running Tests

Install pytest and run all tests from the project root:

```bash
uv venv
uv pip install pytest
uv run pytest
```

Or to run a specific test:

```bash
pytest tests/test_git_pinning/
```

### Publishing
Uploading a source distribution:
1. Get dependencies: `pip3 install build twine`
2. Build sdist: `python -m build`
3. Upload with twine: `twine upload --skip-existing dist/*`
It will prompt for Username and Password, unless you set up ~/.pypirc file:
```
[distutils]
index-servers = pypi
[pypi]
username=__token__
password=<pypi-api-token>
```
Quick build & upload: `./deploy.sh`
