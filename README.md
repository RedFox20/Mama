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


## Who is this for?
Anyone who develops cross-platform C++ libraries or applications which
target Windows, Linux, macOS, iOS, Android, Raspberry.
And anyone who is not satisfied with system-wide dependencies and linker
bugs caused by incompatible system-wide libraries on Linux.


## Who is this not for?
Single platform projects with platform specific build and package management systems
such as Linux exclusive G++ projects or iOS-only cocoapods.


## Setup For Users
1. Get python 3.6 and PIP
2. `$ pip install mama --upgrade`
3. `$ mama init` which creates a `mamafile.py` and patches your CMakeLists.txt
4. (optional) Manual setup: Create your own `mamafile.py` (examples below) and add this to your CMakeLists.txt:
```cmake
include(mama.cmake)
include_directories(${MAMA_INCLUDES})
target_link_libraries(YourProject PRIVATE ${MAMA_LIBS})
```
5. `$ mama build` and enjoy!
6. `$ mama open` to open your project in an IDE


## Command examples
```
    mama init                     Create/Patch initial mamafile.py and CMakeLists.txt
    mama build                    Update and build main project only.
    mama clean                    Cleans main project only.
    mama rebuild                  Cleans, update and build main project only.
    mama update build             Update all dependencies and then build.
    mama build target=dep1        Update and build dep1 only.
    mama configure                Run CMake configuration on main project only.
    mama configure target=all     Run CMake configuration on main project and all deps.
    mama reclone target=dep1      Wipe target dependency completely and clone again.
    mama test                     Run tests on main project.
    mama test target=dep1         Run tests on target dependency project.
```


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
        self.add_git('ReCpp',   'https://github.com/RedFox20/ReCpp.git', branch='master')
        self.add_git('libpng',  'https://github.com/LuaDist/libpng.git')
        self.add_git('libjpeg', 'https://github.com/LuaDist/libjpeg.git')
        self.add_git('glfw',    'https://github.com/glfw/glfw.git')

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


## For Mama Contributors
We are open for any improvements and feedback via pull requests.

You can set up local development with `$ pip install -e .`
Setting up `pypi` configuration for sdist: `$ nano ~/.pypirc`
```
[distutils]
index-servers =
    pypi

[pypi]
repository:https://pypi.python.org/pypi
username=<your-mama-pypy-username>
password=<your-mama-pypy-password>
```
Uploading a source distribution `$ py setup.py sdist upload -r pypi`
