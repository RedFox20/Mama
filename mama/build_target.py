import os.path, shutil
import pathlib, stat, time, subprocess, concurrent.futures
from mama.system import System, console, execute, execute_echo
from mama.util import save_file_if_contents_changed, glob_with_name_match, \
                      normalized_path, write_text_to
from mama.build_dependency import BuildDependency, Git
from mama.build_config import BuildConfig
from mama.cmake_configure import run_cmake_config, run_cmake_build, cmake_default_options, \
                                 cmake_inject_env, cmake_buildsys_flags, cmake_generator
from mama.papa_deploy import papa_deploy_to, Asset
import mama.util as util
from typing import List
from mama.msbuild import msbuild_build

######################################################################################


class BuildTarget:
    """
    Describes a single configurable build target.
    This is the main public interface for configuring a specific target.
    For project-wide configuration, @see BuildConfig in self.config.

    Customization points:
    ```
    class MyProject(mama.BuildTarget):
        
        workspace = 'build'

        def configure(self):
            self.add_git('ReCpp', 
                         'http://github.com/RedFox20/ReCpp.git')

        def configure(self):
            self.add_cmake_options('BUILD_TESTS=ON')

        def package(self):
            self.default_package()
            self.export_asset('extras/meshes/basehead.obj')

        def deploy(self):
            self.papa_deploy('deploy/MyProject')
    ```
    """
    def __init__(self, name, config:BuildConfig, dep:BuildDependency, args:list):
        if config is None: raise RuntimeError(f'BuildTarget {name} config argument must be set')
        if dep is None:    raise RuntimeError(f'BuildTarget {name} dep argument must be set')
        self.config = config
        self.name = name
        self.dep  = dep
        self.args = [] # user defined args for this target (must be a list)
        self.install_target   = 'install'
        self.cmake_ndk_toolchain   = '' # Custom Android toolchain file for this target only
        self.cmake_raspi_toolchain = '' # Custom Raspberry toolchain file for this target only
        self.cmake_ios_toolchain   = '' # Custom iOS toolchain file for this target only
        self.cmake_opts       = []
        self.cmake_cxxflags   = dict()
        self.cmake_cflags     = dict()
        self.cmake_ldflags    = dict()
        self.cmake_build_type = 'Debug' if config.debug else 'RelWithDebInfo'
        self.enable_exceptions = True
        self.enable_unix_make  = False
        self.enable_ninja_build = True and config.ninja_path # attempt to use Ninja
        self.enable_fortran_build = False
        self.enable_cxx_build = True
        self.enable_multiprocess_build = True
        self.gcc_clang_visibility_hidden = True # -fvisibility=hidden 
        self.build_dependencies = [] # dependency files
        self.exported_includes = [] # include folders to export from this target
        self.exported_libs     = [] # libs to export from this target
        self.exported_syslibs  = [] # exported system libraries
        self.exported_assets: List[Asset] = [] # exported asset files
        self.windows = self.config.windows # convenient alias
        self.linux   = self.config.linux
        self.macos   = self.config.macos
        self.ios     = self.config.ios
        self.android = self.config.android
        self.raspi   = self.config.raspi
        self._set_args(args)


    def _set_args(self, args:list):
        if not isinstance(args, list):
            raise RuntimeError(f'BuildTarget {self.name} target args must be a list')
        self.args += args
        #console(f'Added args to {self.name}: {self.args}')


    def source_dir(self, subpath=''):
        """
        Returns the current source directory.
        ```
            self.source_dir()                
            # --> C:/Projects/ReCpp
            self.source_dir('lib/ReCpp.lib') 
            # --> C:/Projects/ReCpp/lib/ReCpp.lib
        ```
        """
        return util.path_join(self.dep.src_dir, subpath)


    def build_dir(self, subpath=''):
        """
        Returns the current build directory.
        ```
            self.build_dir()                
            # --> C:/Projects/ReCpp/build/windows
            self.build_dir('lib/ReCpp.lib')
            # --> C:/Projects/ReCpp/build/windows/lib/ReCpp.lib
        ```
        """
        return util.path_join(self.dep.build_dir, subpath)


    def _get_full_path(self, path):
        if path and not os.path.isabs(path):
            if self.dep.mamafile: # if setting mamafile, then use mamafile folder:
                path = os.path.join(os.path.dirname(self.dep.mamafile), path)
            else:
                path = os.path.join(self.dep.src_dir, path)
            path = normalized_path(path)
        return path


    def _get_mamafile_path(self, name, mamafile):
        if mamafile:
            return self._get_full_path(mamafile)
        maybe_mamafile = self._get_full_path(f'mama/{name}.py')
        if os.path.exists(maybe_mamafile):
            return maybe_mamafile
        return mamafile


    def add_local(self, name, source_dir, mamafile=None, args=[]):
        """
        Add a local dependency. This can be a git submodule or just some local folder.
        which contains its own CMakeLists.txt.
        Optionally you can override the default 'mamafile.py' with your own.

        If the local dependency folder does not contain a `mamafile.py`, you will have to
        provide your own relative or absolute mamafile path.
        ```
        self.add_local('zlib', '3rdparty/zlib')
        self.add_local('zlib', '3rdparty/zlib', mamafile='mama/zlib.py')
        ```
        """
        src      = self._get_full_path(source_dir)
        mamafile = self._get_mamafile_path(name, mamafile)
        dependency = BuildDependency.get(name, self.config, BuildTarget, \
                        workspace=self.dep.workspace, src=src, mamafile=mamafile, args=args)
        self.dep.children.append(dependency)


    def add_git(self, name, git_url, git_branch='', git_tag='', mamafile=None, args=[]):
        """
        Add a remote GIT dependency.
        The dependency will be cloned and updated according to mamabuild.
        Use `mama update` to force update the git repositories.
    
        If the remote GIT repository does not contain a `mamafile.py`, you will have to
        provide your own relative or absolute mamafile path.
    
        Any arguments are passed onto child targets as `self.args`.
        ```
        self.add_git('ReCpp', 'https://github.com/RedFox20/ReCpp.git')
        self.add_git('ReCpp', 'https://github.com/RedFox20/ReCpp.git', 
                     git_branch='master')
        self.add_git('opencv', 'https://github.com/opencv/opencv.git', 
                     git_branch='3.4', mamafile='mama/opencv_cfg.py')
        ```
        """
        git = Git(git_url, git_branch, git_tag)
        mamafile = self._get_mamafile_path(name, mamafile)
        dependency = BuildDependency.get(name, self.config, BuildTarget, \
                        workspace=self.dep.workspace, git=git, mamafile=mamafile, args=args)
        self.dep.children.append(dependency)


    def get_dependency(self, name):
        """
        Finds a child dependency by name.
        ```
            zlib_dep = self.get_dependency('zlib')
        ```
        """
        if self.dep.name == name:
            return self.dep
        for dep in self.dep.children:
            if dep.name == name:
                return dep
        raise KeyError(f"BuildTarget {self.name} has no child dependency named '{name}'")


    def find_target(self, name):
        """
        Finds a child BuildTarget by name.
        ```
            zlib = self.find_target('zlib')
        ```
        """
        if self.name == name:
            return self
        for dep in self.dep.children:
            if dep.name == name:
                return dep.target
        raise KeyError(f"BuildTarget {self.name} has no child target named '{name}'")


    def inject_products(self, dst_dep, src_dep, include_path, libs, libfilters=None):
        """
        Injects products from `src_dep` into `dst_dep` as CMake defines.
        Name of defines is given via `include_path` and `libs` params.
        `libfilters` does simple string matching; if nothing matches, the first export lib is chosen.
        ```
        self.inject_products('libpng', 'zlib', 
                             'ZLIB_INCLUDE_DIR', 'ZLIB_LIBRARY',
                             'zlibstatic')
        ```
        """
        dst_dep = self.get_dependency(dst_dep)
        src_dep = self.get_dependency(src_dep)
        dst_dep.depends_on.append(src_dep)
        dst_dep.product_sources.append( (src_dep, include_path, libs, libfilters) )


    def get_product_defines(self):
        """
        Collects all results injected by `inject_products()`.
        Returns a list of injected defines:
        ```
            defines = self.get_product_defines()
            # --> [ 'ZLIB_INCLUDE_DIR=path/to/zlib/include', 
            #       'ZLIB_LIBRARY=path/to/lib/zlib.a', ... ]
        ```
        """
        defines = []
        for source in self.dep.product_sources:
            srcdep    = source[0]
            includes  = srcdep.target._get_exported_includes()
            libraries = srcdep.target._get_exported_libs(source[3])
            #console(f'grabbing products: {srcdep.name}; includes={includes}; libraries={libraries}')
            defines.append(f'{source[1]}={includes}')
            defines.append(f'{source[2]}={libraries}')
        return defines


    def _get_exported_includes(self):
        return ';'.join(self.exported_includes) if self.exported_includes else ''


    def _get_exported_libs(self, libfilters):
        #console(f'_get_exported_libs: libs={self.exported_libs} syslibs={self.exported_syslibs}')
        libs = []
        if self.exported_libs:
            if libfilters:
                for lib in self.exported_libs:
                    if libfilters in lib: libs.append(lib)
                # if no matches with libfilters, just append the first
                if not libs: libs.append(self.exported_libs[0])
            else:
                libs = self.exported_libs
        return ';'.join(libs)


    def get_target_products(self, target_name):
        """
        Gets target products as a tuple: (include_paths=[], libs=[])
        ```
            zlib_inc, zlib_libs = self.get_target_products('zlib')
        ```
        """
        dep = self.get_dependency(target_name)
        target:BuildTarget = dep.target
        return (target._get_exported_includes(), target._get_exported_libs())


    def add_build_dependency(self, all=None, windows=None, linux=None, macos=None, ios=None, android=None):
        """
        Manually add a build dependency to prevent unnecessary rebuilds.

        @note Normally the build dependency is detected from the packaged libraries.
        
        if the dependency file does not exist, then the project will be rebuilt
        
        if your project has no build dependencies, it will always be rebuilt, so make sure
        to add_build_dependency or export_lib
        ```
            # Note: relative to build directory
            self.add_build_dependency('customProduct.dat')
        ```
        """
        dependency = all if all else self.select(windows, linux, macos, ios, android)
        if dependency:
            dependency = normalized_path(os.path.join(self.dep.build_dir, dependency))
            self.build_dependencies.append(dependency)
            #console(f'    {self.name}.build_dependencies += {dependency}')


    def _get_root_path(self, path, src_dir):
        root = self.dep.src_dir if src_dir else self.dep.build_dir
        return normalized_path(os.path.join(root, path))


    def export_include(self, include_path, build_dir=False):
        """
        CUSTOM PACKAGE INCLUDES (if self.default_package() is insufficient).
        
        Export include path relative to source directory OR if build_dir=True, then relative to build directory.
        ```
            self.export_include('include')  # MyRepo/include

            # CMake installed includes in build/installed/MyLib/include
            self.export_include('installed/MyLib/include', build_dir=True)
        ```
        """
        include_path = self._get_root_path(include_path, not build_dir)
        #console(f'export_include={include_path}')
        if os.path.exists(include_path):
            if not include_path in self.exported_includes:
                self.exported_includes.append(include_path)
            return True
        return False


    def export_includes(self, include_paths=[''], build_dir=False):
        """
        CUSTOM PACKAGE INCLUDES (if self.default_package() is insufficient)
        
        Export include paths relative to source directory
        OR if build_dir=True, then relative to build directory
        Example:
        ```
        self.export_includes(['include', 'src/moreincludes'])
        self.export_includes(['installed/include', 'installed/src/moreincludes'], build_dir=True)
        ```
        """
        self.exported_includes = []
        for include_path in include_paths:
            self.export_include(include_path, build_dir)


    def export_lib(self, relative_path, src_dir=False):
        """
        CUSTOM PACKAGE LIBS (if self.default_package() is insufficient)
        
        Export a specific lib relative to build directory
        OR if src_dir=True, then relative to source directory
        Example:
        ```
        self.export_lib('mylib.a')                    # from build dir
        self.export_lib('lib/mylib.a', src_dir=True)  # from project source dir
        ```
        """
        path = self._get_root_path(relative_path, src_dir)
        if os.path.exists(path):
            self.exported_libs.append(path)
            self.exported_libs = self._get_unique_basenames(self.exported_libs)


    def export_libs(self, path = '.', pattern_substrings = ['.lib', '.a'], src_dir=False, order=None):
        """
        CUSTOM PACKAGE LIBS (if self.default_package() is insufficient)
        
        Export several libs relative to build directory using EXTENSION MATCHING
        OR if src_dir=True, then relative to source directory
        
        Example:
        ```
        self.export_libs()                     # gather any .lib or .a from build dir
        self.export_libs('.', ['.dll', '.so']) # gather any .dll or .so from build dir
        self.export_libs('lib', src_dir=True)  # export everything from project/lib directory
        self.export_libs('external/lib')       # gather specific static libs from build dir
        
        # export the libs in a particular order for Linux linker
        self.export_libs('lib', order=[
            'xphoto', 'calib3d', 'flann', 'core'
        ])
        -->  [..others.., libopencv_xphoto.a, libopencv_calib3d.a, libopencv_flann.a, libopencv_core.a]
        ```
        """
        libs = glob_with_name_match(self._get_root_path(path, src_dir), pattern_substrings)
        if order:
            def lib_index(lib):
                for i in range(len(order)):
                    if order[i] in lib: return i
                return -1
            def sort_key(lib):
                return lib_index(lib)
            libs.sort(key=sort_key)
        self.exported_libs += libs
        self.exported_libs = self._get_unique_basenames(self.exported_libs)
        return len(self.exported_libs) > 0


    def export_asset(self, asset, category=None, src_dir=True):
        """
        Exports a single asset file from this target
        This can be later used when creating a deployment

        category -- (optional) Can be used for grouping the assets and flattening folder structure
        
        Example:
        ```
        self.export_asset('extras/csharp/NanoMesh.cs')
            --> {deploy}/extras/csharp/NanoMesh.cs

        self.export_asset('extras/csharp/NanoMesh.cs', category='dotnet')
            --> {deploy}/dotnet/NanoMesh.cs
        ```
        """
        full_asset = self._get_root_path(asset, src_dir)
        if os.path.exists(full_asset):
            self.exported_assets.append(Asset(asset, full_asset, category))
            return True
        return False


    def export_assets(self, assets_path, pattern_substrings = [], category=None, src_dir=True):
        """
        Performs a GLOB recurse, using specific pattern substrings.
        This can be later used when creating a deployment

        category -- (optional) Can be used for grouping the assets and flattening folder structure
        
        Example:
        ```
        self.export_assets('extras/csharp', ['.cs'])
            --> {deploy}/extras/csharp/NanoMesh.cs
        
        self.export_assets('extras/csharp', ['.cs'], category='dotnet')
            --> {deploy}/dotnet/NanoMesh.cs
        ```
        """
        assets_path += '/'
        assets = glob_with_name_match(self._get_root_path(assets_path, src_dir), pattern_substrings, match_dirs=False)
        if assets:
            for full_asset in assets:
                self.exported_assets.append(Asset(assets_path, full_asset, category))
            return True
        return False


    def export_syslib(self, name, required=True):
        """
        For UNIX: Find and export system libraries so they are automatically linked with mamabuild.

        :returns: TRUE if syslib was exported; FALSE if required=False and syslib not found
        ```
            self.export_syslib('uuid')
            # will attempt to find system library in this order:
            #   1. uuid
            #   2. libuuid.so
            #   3. libuuid.a
        ```
        """
        if self.ios or self.macos:
            if not name.startswith('-framework '):
                raise EnvironmentError(f'Expected "-framework name" but got "{name}"')
            lib = name
        elif self.linux:
            lib = f'/usr/lib/x86_64-linux-gnu/{name}'
            if not os.path.exists(lib):
                lib = f'/usr/lib/x86_64-linux-gnu/lib{name}.so'
            if not os.path.exists(lib):
                lib = f'/usr/lib/x86_64-linux-gnu/lib{name}.a'
            if not os.path.exists(lib):
                if not required: return False
                raise IOError(f'Error {self.name} failed to find REQUIRED SysLib: {name}')
        else:
            lib = name # just export it. expect system linker to find it.
        #console(f'Exporting syslib: {name}:{lib}')
        self.exported_syslibs.append(lib)
        self.exported_syslibs = self._get_unique_basenames(self.exported_syslibs)
        return True


    def _get_unique_basenames(self, items):
        unique = dict()
        for item in items:
            if isinstance(item, tuple):
                unique[os.path.basename(item[0])] = item
            elif item.startswith('-framework '):
                unique[item.split(' ', 1)[1]] = item
            else:
                unique[os.path.basename(item)] = item
        return list(unique.values())


    def inject_env(self):
        """
        Injects default platform and target specific environment variables.
        This can be used when performing full custom build step:
        ```
            def build(self):
                self.inject_env()       # prepare platform
                self.my_custom_build()  # 
        ```
        """
        cmake_inject_env(self)


    def _add_dict_flag(self, dest:dict, flag):
        if not flag: return
        if ' ' in flag:
            for subflag in flag.split(' '):
                self._add_dict_flag(dest, subflag)
        elif '=' in flag:
            key, value = flag.split('=', 1)
            dest[key] = value
        elif ':' in flag:
            key, value = flag.split(':', 1)
            dest[key] = value
        else:
            dest[flag] = ''


    def add_cxx_flags(self, *flags):
        """
        Adds C++ flags for compilation step.
        Supports many different usages: strings, list of strings, kwargs, or space separate string.
        ```
            self.add_cxx_flags('-Wall')
            self.add_cxx_flags(['-Wall', '-std=c++17'])
            self.add_cxx_flags('-Wall', '-std=c++17')
            self.add_cxx_flags('-Wall -std=c++17')
        ```
        """
        for flag in flags:
            if isinstance(flag, list): self.add_cxx_flags(*flag)
            else: self._add_dict_flag(self.cmake_cxxflags, flag)
    

    def add_c_flags(self, *flags):
        """
        Adds C flags for compilation step.
        Supports many different usages: strings, list of strings, kwargs, or space separate string.
        ```
            self.add_cxx_flags('-Wall')
            self.add_cxx_flags(['-Wall', '-std=c99'])
            self.add_cxx_flags('-Wall', '-std=c99')
            self.add_cxx_flags('-Wall -std=c99')
        ```
        """
        for flag in flags:
            if isinstance(flag, list): self.add_c_flags(*flag)
            else: self._add_dict_flag(self.cmake_cflags, flag)
    

    def add_cl_flags(self, *flags):
        """
        Adds C AND C++ flags for compilation step.
        Supports many different usages: strings, list of strings, kwargs, or space separate string.
        ```
            self.add_cxx_flags('-Wall')
            self.add_cxx_flags(['-Wall', '-march=native'])
            self.add_cxx_flags('-Wall', '-march=native')
            self.add_cxx_flags('-Wall -march=native')
        ```
        """
        for flag in flags:
            if isinstance(flag, list): self.add_cl_flags(*flag)
            else:
                self._add_dict_flag(self.cmake_cxxflags, flag)
                self._add_dict_flag(self.cmake_cflags, flag)


    def add_ld_flags(self, *flags):
        """
        Adds flags for linker step; No platform checking is done.
        Supports many different usages: strings, list of strings, kwargs, or space separate string
        ```
            self.add_ld_flags('-rdynamic')
            self.add_ld_flags(['-rdynamic', '-s'])
            self.add_ld_flags('-rdynamic', '-s')
            self.add_ld_flags('-rdynamic -s')
        ```
        """
        for flag in flags:
            if isinstance(flag, list): self.add_ld_flags(*flag)
            else: self._add_dict_flag(self.cmake_ldflags, flag)


    def add_platform_cxx_flags(self, windows=None, linux=None, macos=None, ios=None, android=None):
        """
        Adds C / C++ flags flags depending on configuration platform.
        Supports many different usages: strings, list of strings, kwargs, or space separate string.
        ```
            self.add_cxx_flags('-Wall')
            self.add_cxx_flags(['-Wall', '-std=c++17'])
            self.add_cxx_flags('-Wall', '-std=c++17')
            self.add_cxx_flags('-Wall -std=c++17')
        ```
        """
        flags = self.select(windows, linux, macos, ios, android)
        if flags: self.add_cxx_flags(flags)


    def add_platform_ld_flags(self, windows=None, linux=None, macos=None, ios=None, android=None):
        """
        Adds linker flags depending on configuration platform.
        Supports many different usages: strings, list of strings, or space separate string.
        ```
            self.add_platform_ld_flags(windows='/LTCG', 
                                    ios=['-lobjc', '-rdynamic'],
                                    linux='-rdynamic -s')
        ```
        """
        flags = self.select(windows, linux, macos, ios, android)
        if flags: self.add_ld_flags(flags)


    def add_cmake_options(self, *options):
        """
        Main method for configuring CMake options.
        ```
            self.add_cmake_options('ZLIB_STATIC=TRUE', 'NO_GUI=1')
            self.add_cmake_options(['ZLIB_STATIC=TRUE', 'NO_GUI=1'])
        ```
        """
        for option in options:
            if isinstance(option, list): self.cmake_opts += option
            else:                        self.cmake_opts.append(option)

    
    def add_platform_options(self, windows=None, linux=None, macos=None, ios=None, android=None):
        """
        Selectively applies CMake options depending on configuration platform.
        ```
            self.add_platform_options(windows='ZLIB_STATIC=TRUE')
        ```
        """
        defines = self.select(windows, linux, macos, ios, android)
        if defines: self.cmake_opts += defines


    def select(self, windows, linux, macos, ios, android):
        if   self.windows and windows: return windows
        elif self.linux   and linux:   return linux
        elif self.macos   and macos:   return macos
        elif self.ios     and ios:     return ios
        elif self.android and android: return android
        return None


    def enable_cxx20(self):
        """Enable a specific C++ standard"""
        self.cmake_cxxflags['/std' if self.windows else '-std'] = 'c++latest' if self.windows else 'c++2a'
    
    
    def enable_cxx17(self):
        """Enable a specific C++ standard"""
        self.cmake_cxxflags['/std' if self.windows else '-std'] = 'c++17'


    def enable_cxx14(self):
        """Enable a specific C++ standard"""
        self.cmake_cxxflags['/std' if self.windows else '-std'] = 'c++14'


    def enable_cxx11(self):
        """Enable a specific C++ standard"""
        self.cmake_cxxflags['/std' if self.windows else '-std'] = 'c++11'


    def copy_built_file(self, builtFile, copyToFolder):
        """
        Utility for copying files within the build directory.
        ```
            self.copy_built_file('RelWithDebInfo/libawesome.a', 'lib')
        ```
        """
        src = f'{self.dep.build_dir}/{builtFile}'
        dst = f'{self.dep.build_dir}/{copyToFolder}'
        shutil.copy(src, dst)


    def download_file(self, remote_url, local_dir, force=False):
        """
        Downloads a file if it doesn't already exist.
        ```
            self.download_file('http://example.com/file1', 'bin')
            # --> 'bin/file1'
        ```
        """
        util.download_file(remote_url, local_dir, force)


    def download_and_unzip(self, remote_zip, extract_dir, unless_file_exists=None):
        """
        Downloads and unzips an archive if it doesn't already exist.

        unless_file_exists -- If the specified file exists, then download and unzip steps are skipped.
        ```
            self.download_and_unzip('http://example.com/archive.zip', 
                                    'bin', 'bin/unzipped_file.txt')
        ```
        """
        util.download_and_unzip(remote_zip, extract_dir, unless_file_exists)


    def visibility_hidden(self, hidden=True):
        """
        Whether to pass `-fvisibility=hidden` to GCC and Clang compilers. Default is `True`.
        ```
            self.visibility_hidden(False)
        ```
        """
        self.gcc_clang_visibility_hidden = hidden


    def disable_ninja_build(self):
        """
        Use this to completely disable Ninja build for this target
        By default, if Ninja build is detected, non-MSVC builds use Ninja for faster builds.
        Use this if you want to, for example, generate Xcode project:
        ```
            if self.ios or self.macos:
                self.disable_ninja_build()
        ```
        """
        self.enable_ninja_build = False


    def enable_fortran(self, path=''):
        """
        Enable fortran for this target only
        path -- Optional custom path or command for the Fortran compiler
        ```
            self.enable_fortran()   # attempt to autodetect fortran
            self.enable_fortran('/SysGCC/bin/gfortran')  # specify fortran explicitly
        ```
        """
        self.config.enable_fortran(path)
        self.enable_fortran_build = True


    def disable_cxx_compiler(self):
        """
        Disable any C++ options and C++ compiler configuration
        ```
            def configure(self):
                self.disable_cxx_compiler()
        ```
        """
        self.enable_cxx_build = False


    def nothing_to_build(self):
        """
        Call this to completely skip the build step every time and instead.
        ```
            def dependencies(self):
                self.nothing_to_build()
        ```
        """
        self.dep.nothing_to_build = True
        self.dep.should_rebuild = False


    def run(self, command, src_dir=False):
        """
        Run a command in the build or source folder.
        Can be used for any custom commands or custom build systems.
        src_dir -- [False] If true, then command is relative to source directory.
        ```
            self.run('./configure')
            self.run('make release -j7')
        ```
        """
        dir = self.dep.src_dir if src_dir else self.dep.build_dir
        execute(f'cd {dir} && {command}', echo=True)


    def run_program(self, working_dir, command):
        """
        Run any program in any directory. Can be used for custom tools.
        ```
            self.run_program(self.source_dir('bin'), 
                             self.source_dir('bin/DbTool'))
        ```
        """
        execute_echo(working_dir, command)

    
    def gdb(self, command, src_dir=True):
        """
        Run a command with gdb in the build folder.
        ```
            self.gdb('bin/NanoMeshTests')
        ```
        """
        if self.android or self.ios or self.raspi:
            console('Cannot run tests for Android, iOS, Raspi builds.')
            return # nothing to run
        
        split = command.split(' ', 1)
        cmd = split[0].lstrip('.')
        args = split[1] if len(split) >= 2 else ''
        path = self.dep.src_dir if src_dir else self.dep.build_dir
        path = f"{path}/{os.path.dirname(cmd).lstrip('/')}"
        exe = os.path.basename(cmd)

        if self.windows:
            if not src_dir: path = f'{path}/{self.cmake_build_type}'
            gdb = exe
        elif self.macos:
            # b: batch, q: quiet, -o r: run
            # -k bt: on crash, backtrace
            # -k q: on crash, quit 
            gdb = f'lldb -b -o r -k bt -k q  -- ./{exe} {args}'
        else: # linux
            # r: run;  bt: give backtrace;  q: quit when done;
            gdb = f'gdb -batch -return-child-result -ex=r -ex=bt -ex=q --args ./{exe} {args}'

        if not (os.path.exists(f'{path}/{exe}') or os.path.exists(f'{path}/{exe}.exe')):
            raise IOError(f'Could not find {path}/{exe}')
        execute_echo(path, gdb)


    ########## Customization Points ###########


    def dependencies(self):
        """
        Add any dependencies in this step.
        ```
        class MyProject(mama.BuildTarget):
            def dependencies(self):
                self.add_git('ReCpp',
                             'http://github.com/RedFox20/ReCpp.git')
                self.add_local('fbxsdk',
                               'third_party/FBX')
        ```
        """
        pass


    def configure(self):
        """
        Perform any pre-build steps here.
        ```
        class MyProject(mama.BuildTarget):
            def configure(self):
                self.add_cmake_options('BUILD_TESTS=ON')
        ```
        """
        pass


    def build(self):
        """
        Build this target. By default it uses CMake build.
        """
        self.cmake_build()


    def clean(self):
        """
        Perform any pre-clean steps here.
        """
        pass


    def disable_install(self):
        """
        Sets self.install_target to None, which disables the CMake install step.
        """
        self.install_target = ''


    def install(self):
        """
        Perform custom install steps here. By default it uses CMake install.
        """
        self.cmake_install()


    def package(self):
        """
        Perform any post-build steps to package the products.
        If no headers or libs are exported, then `default_package()` will be run instead

        Every library should at least export some headers.
        ```
        def package(self):
            # use the built-in default packing
            self.default_package()
            # custom export AGL as include from source folder
            self.export_includes(['AGL'])
            # custom export any .lib or .a from build folder
            self.export_libs('.', ['.lib', '.a']) 
            
            if self.windows:
                self.export_syslib('opengl32.lib')

            # export some asset from source folder
            self.export_asset('extras/meshes/basehead.obj')
        ```
        """
        pass
    

    def default_package(self):
        """
        Performs default packaging steps.
        This is called if self.package() did not export anything.
        It can also be called manually to collect includes and libs.
        ```
        def package(self):
            self.default_package()
        ```
        """
        # try multiple common/popular C and C++ library include patterns
        if   self.export_include('include', build_dir=True):  pass
        elif self.export_include('include', build_dir=False): pass
        elif self.export_include('src',     build_dir=False): pass
        elif self.export_include('',        build_dir=False): pass

        # default export from {build_dir}/{cmake_build_type}
        if self.export_libs(self.cmake_build_type, src_dir=False): pass
        elif self.export_libs('lib', src_dir=False): pass
        elif self.export_libs('.', src_dir=False): pass
    

    def deploy(self):
        """
        Custom deployment stage. Built in support for PAPA packages:
        ```
        def deploy(self):
            self.papa_deploy('deploy/NanoMesh')
        ```
        """
        pass
    
    
    def papa_deploy(self, package_path, src_dir=True, 
                    recurse_syslibs=False, recurse_assets=False):
        """
        This will create a PAPA package, which includes
            package_path/papa.txt
            package_path/{includes}
            package_path/{libs}
            package_path/{assets}

        src_dir -- Whether package will be deployed to src dir or build dir
        recurse_syslibs -- Whether to include system libraries from child dependencies
        recurse_assets -- Whether to include assets from child dependencies

        Example: `self.papa_deploy('MyPackageName')`

        PAPA package structure:
            MyPackageName/papa.txt
            MyPackageName/libawesome.so
            MyPackageName/include/...
            MyPackageName/someassets/extra.txt
            
        PAPA descriptor `papa.txt` format:
            P MyPackageName
            I include
            L libawesome.so
            S libGL.a
            A someassets/extra.txt
        """
        path = self._get_root_path(package_path, src_dir)
        syslibs = self._recurse_syslibs() if recurse_syslibs else self.exported_syslibs
        assets  = self._recurse_assets()  if recurse_assets  else self.exported_assets
        papa_deploy_to(path, self.exported_includes, self.exported_libs, syslibs,  assets)


    def _recurse_syslibs(self):
        syslibs = []
        def append_syslibs(target: BuildTarget):
            nonlocal syslibs
            syslibs += target.exported_syslibs
            for child in target.dep.children:
                append_syslibs(child.target)
        append_syslibs(self)
        return syslibs


    def _recurse_assets(self):
        assets = []
        def append_assets(target: BuildTarget):
            nonlocal assets
            assets += target.exported_assets
            for child in target.dep.children:
                append_assets(child.target)
        append_assets(self)
        return assets        


    def test(self, args):
        """
        Perform test steps here with test args.
        `mama test arg1 arg2 arg3`
        ```
            def test(self, args):
                self.gdb(f'RppTests {args}')
        ```
        """
        pass


    def start(self, args):
        """
        Start a custom process through mama
        `mama start=arg`
        ```
        def start(self, args):
            if 'dbtool' in args:
                self.run_program(self.source_dir('bin'),
                                 self.source_dir('bin/DbTool'))
        ```
        """
        pass


    ############################################


    def cmake_install(self):
        if self.config.print:
            console('\n\n#############################################################')
            console(f"CMake install {self.name} ...")
        run_cmake_build(self, install=True)


    def clean_target(self):
        self.dep.clean()


    def cmake_build(self):
        if self.config.print:
            console('\n\n#############################################################')
            console(f"CMake build {self.name}")
        self.dep.ensure_cmakelists_exists()
        def cmake_flags():
            flags = ''
            options = self.cmake_opts + cmake_default_options(self) + self.get_product_defines()
            for opt in options: flags += '-D'+opt+' '
            return flags

        self.inject_env()
        run_cmake_config(self, cmake_flags())
        run_cmake_build(self, install=True, extraflags=cmake_buildsys_flags(self))


    def is_test_target(self):
        """
        TRUE if this build target was specified along with test command:
        `mama test target=this_name`
        """
        return self.config.test and self.dep.is_root_or_config_target()


    ## Build only this target
    def _execute_tasks(self):
        if self.dep.already_executed:
            return
        try:
            self.dep.already_executed = True

            if self.dep.should_rebuild and not self.dep.nothing_to_build:
                self.configure() # user customization
                if not self.dep.nothing_to_build:
                    self.build() # user customization
                    self.dep.successful_build()

            self.package() # user customization

            # no packaging provided by user; use default packaging instead
            if not self.exported_includes or not (self.exported_libs or self.exported_syslibs):
                self.default_package()

            # only save and print exports if we built anything
            if self.dep.build_dir_exists():
                self.dep.save_exports_as_dependencies(self.exported_libs)
                self._print_exports()

            self.deploy() # user customization

            if self.is_test_target():
                test_args = self.config.test.lstrip()
                if self.config.print: console(f'  - Testing {self.name} {test_args}')
                self.test(test_args)

            if self.dep.is_root and self.config.start:
                start_args = self.config.start.lstrip()
                if self.config.print: console(f'  - Starting {self.name} {start_args}')
                self.start(start_args)
        except:
            console(f'  [BUILD FAILED]  {self.dep.name}')
            raise


    def _print_ws_path(self, what, path, check_exists=True):
        def exists():
            return '' if os.path.exists(path) else '   !! (path does not exist) !!' 
        if path.startswith('-framework'):
            console(f'    {what}  {path}')
        elif path.startswith(self.config.workspaces_root):
            console(f'    {what}  {path[len(self.config.workspaces_root) + 1:]}{exists()}')
        elif path.startswith(self.dep.src_dir):
            console(f'    {what}  {path[len(self.dep.src_dir) + 1:]}{exists()}')
        else:
            ex = exists() if check_exists else ''
            console(f'    {what}  {path}{ex}')


    def _print_exports(self):
        if not self.config.print:
            return
        console(f'  - Package {self.name}')
        for include in self.exported_includes: self._print_ws_path('<I>', include)
        for library in self.exported_libs:     self._print_ws_path('[L]', library)
        for library in self.exported_syslibs:  self._print_ws_path('[S]', library, check_exists=False)
        for asset   in self.exported_assets:   self._print_ws_path('[A]', asset.outpath, check_exists=False)


    ############################################


    def ms_build(self, projectfile, properties:dict = dict()):
        """
        Invokes MSBuild on the specificied projectfile and passes specified
        properties to MSBuild.
        ```
        def build(self):
            self.cmake_build()
            self.ms_build('extras/csharp/CSharpTests.sln', {
                'Configuration': 'Debug',
                'Platform': 'Any CPU',
            })
        ```
        Default properties set by Mama if not specified via properties dict:
        /p:PreferredToolArchitecture=x64
        /p:Configuration=Release
        /p:Platform=x64
        """
        msbuild_build(self.config, projectfile, properties)


######################################################################################
        