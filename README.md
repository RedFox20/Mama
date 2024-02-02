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
target any combination of [Windows, Linux, macOS, iOS, Android, Raspberry, Oclea, MIPS].
And anyone who is not satisfied with system-wide dependencies and linker
bugs caused by incompatible system-wide libraries on Linux.

If you require an easy to use, reproducible project/namespace scoped package+build system, this is for you.
Your builds will not rely on hard to setup system packages, all you need to do is type `mama build`.

### Supported platforms ###
- Windows (64-bit x86_64, 32-bit x86, 64-bit arm64, 32-bit armv7) default is latest MSVC
- Linux (Ubuntu) (64-bit x86_64, 32-bit x86) both GCC and Clang
- MacOS (64-bit x86_64, 64-bit arm64) via config.macos_version
- iOS (64-bit arm64) via config.ios_version
- Android (64-bit arm64, 32-bit armv7) via env ANDROID_NDK_HOME or ANDROID_HOME
- Raspberry (32-bit armv7) via env RASPI_HOME
- Oclea (64-bit arm64) via config.set_oclea_toolchain()
- MIPS (mips mipsel, mips64, mips64el) via config.set_mips_toolchain()

## Who is this NOT for?
Single platform projects with platform specific build configuration and system wide dependency management
such as Linux exclusive G++ projects using apt-get libraries or iOS-only apps using cocoapods.


## Artifactory
Provides a mechanism to upload pre-built packages to a private artifactory server through `mama upload mypackage`. These packages will be automatically used if a git:package commit hash matches.


## Setup For Users
1. Get python 3.6 and PIP
2. `$ pip install mama --upgrade`
3. `$ cd yourproject`
3. `$ mama init` which creates a `mamafile.py` and patches your CMakeLists.txt
4. (optional) Manual setup: Create your own `mamafile.py` (examples below) and add this to your CMakeLists.txt:
```cmake
include(mama.cmake)
include_directories(${MAMA_INCLUDES})
target_link_libraries(YourProject PRIVATE ${MAMA_LIBS})
```
5. `$ mama build` and enjoy!
6. `$ mama open` to open your project in an IDE / VSCode


## Command examples
```
  mama init                      Initialize a new project. Tries to create mamafile.py and CMakeLists.txt
  mama build                     Update and build main project only. This only clones, but does not update!
  mama build x86 opencv          Cross compile build target opencv to x86 architecture
  mama build android             Cross compile to arm64 android NDK
  mama build android-26 arm      Cross compile to armv7 android NDK API level 26
  mama update                    Update all dependencies by doing git pull and build.
  mama clean                     Cleans main project only.
  mama clean x86 opencv          Cleans main project only.
  mama clean all                 Cleans EVERYTHING in the dependency chain for current arch.
  mama rebuild                   Cleans, update and build main project only.
  mama build dep1                Update and build dep1 only.
  mama update dep1               Update and build the specified target.
  mama serve android             Update, build and deploy for Android
  mama wipe dep1                 Wipe target dependency completely and clone again.
  mama upload dep1               Deploys and uploads dependency to Artifactory server.
  mama test                      Run tests on main project.
  mama test=arg                  Run tests on main project with an argument.
  mama test="arg1 arg2"          Run tests on main project with multiple arguments.
  mama test dep1                 Run tests on target dependency project.
  mama dep1 start=dbtool         Call target project mamafile start() with args [`dbtool`].
```
Call `mama help` for more usage information.

## Mamafile examples

Project `AlphaGL/mamafile.py`
```py
import mama
class AlphaGL(mama.BuildTarget):
    # where to build intermediates
    workspace = 'build' # for system-wide workspace, use: global_workspace = 'mycompany'

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


## For Mama Contributors
We are open for any improvements and feedback via pull requests.

The package `setuptools>=65.0` is required, ensure the version is correct with `pip3 show setuptools`.

You can set up local development with `$ pip3 install -e . --no-cache-dir` but make sure you have latest setuptools (>65.0) and latest pip3 (>22.3). This command will fail with older toolkits.

Uploading a source distributionP:
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
Quick build & upload using Python 3.9: `./deploy.sh`
