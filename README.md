# Mama Build Tool
Mama - A modular C++ build tool even your mama can use

The main goal of this project is to provide extremely convenient in-source builds
for cross platform projects. Building is as simple as `mama build windows` - no ceremony~!

CMake projects with trivial configurations and no dependencies can be handled
automatically by Mama. This makes header-only libraries or stand-alone C libraries
extremely easy to link.

Adding projects with already configured `mamafile.py` is trivial


## Setup For Users
1. Get python 3.6
2. `$ pip install mama`
3. Create `mamafile.py` for your project
4. Add this to your CMakeLists.txt:
```cmake
include(mama.cmake)
include_directories(${MAMA_INCLUDES})
target_link_libraries(YourProject PRIVATE ${MAMA_LIBS})
```
5. `$ mama build` and enjoy!
6. `$ mama open` to open your project in an IDE
7. Upgrading mama: `$ pip install --upgrade mama`

## Command examples
```
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
    # this defines where to build all the dependencies
    local_workspace = 'build'

    # grab dependencies straight from git repositories
    # if the projects are trivial, then no extra configuration is needed
    def dependencies(self):
        self.add_git('ReCpp',   'https://github.com/RedFox20/ReCpp.git')
        self.add_git('libpng',  'https://github.com/LuaDist/libpng.git')
        self.add_git('libjpeg', 'https://github.com/LuaDist/libjpeg.git')
        self.add_git('glfw',    'https://github.com/glfw/glfw.git')

    # optional: customize package exports if repository doesn't have `include` or `src`
    def package(self):
        self.export_libs('.', ['.lib', '.a']) # export any .lib or .a from build folder
        self.export_includes(['AGL']) # export AGL as include from source folder

    def test(self):
        pass
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
        self.add_cmake_options(f'ZLIB_INCLUDE_DIR={zinclude[0]}')
        self.add_cmake_options(f'ZLIB_LIBRARY={zlibrary[0]}')
        self.add_cmake_options('BUILD_SHARED_LIB=NO', 'PNG_TESTS=NO')

    def package(self):
        # libpng builds its stuff into `{build}/lib`
        self.export_libs('lib', ['.lib', '.a'])
        # export installed include path from build dir
        self.export_include('include', build_dir=True)
```


## For Developers
Set up local development with `$ pip install -e .`
Setting up `pypi` configuration: `$ nano ~/.pypirc`
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
