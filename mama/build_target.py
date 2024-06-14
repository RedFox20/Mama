from __future__ import annotations
from typing import List, TYPE_CHECKING
import os.path, time

from .types.git import Git
from .types.local_source import LocalSource
from .types.asset import Asset
from .types.artifactory_pkg import ArtifactoryPkg

from .artifactory import artifactory_fetch_and_reconfigure
from .utils.system import System, console
from .utils.gdb import run_gdb, filter_gdb_arg
from .utils.gtest import run_gtest
from .utils.run import run_in_project_dir, run_in_working_dir, run_in_command_dir
from .utils.gnu_project import GnuProject
from .papa_deploy import papa_deploy_to
from .papa_upload import papa_upload_to
import mama.msbuild as msbuild
import mama.util as util
import mama.cmake_configure as cmake
import mama.package as package

if TYPE_CHECKING:
    from .build_config import BuildConfig
    from .build_dependency import BuildDependency


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
    def __init__(self, name, config:BuildConfig, dep:BuildDependency, args:List[str]):
        if config is None: raise RuntimeError(f'BuildTarget {name} config argument must be set')
        if dep is None:    raise RuntimeError(f'BuildTarget {name} dep argument must be set')
        self.config = config
        self.name = name
        self.dep  = dep
        self.args = [] # user defined args for this target (must be a list)
        self.install_target = 'install'
        self.version = ''  # Custom version string for packaging step
        self.cmake_ndk_toolchain   = '' # Custom Android toolchain file for this target only
        self.cmake_raspi_toolchain = '' # Custom Raspberry toolchain file for this target only
        self.cmake_ios_toolchain   = '' # Custom iOS toolchain file for this target only
        self.cmake_opts       = []
        self.cmake_cxxflags   = dict()
        self.cmake_cflags     = dict()
        self.cmake_ldflags    = dict()
        self.cmake_build_type = 'Debug' if config.debug else 'RelWithDebInfo'
        self.cmake_lists_path = 'CMakeLists.txt' # can be relative to src_dir (default), or absolute
        self.enable_exceptions = True
        self.enable_unix_make  = False
        self.enable_ninja_build = True and config.ninja_path # attempt to use Ninja
        self.enable_fortran_build = False
        self.enable_cxx_build = True
        self.enable_multiprocess_build = True
        self.clean_intermediate_files = False # force delete .o and .obj files after build success
        self.gcc_clang_visibility_hidden = True # -fvisibility=hidden
        self.build_products = [] # executables/libs products from last build
        self.no_includes = False # no includes to export
        self.no_libs = False # no libs to export
        self.exported_includes = [] # include folders to export from this target
        self.exported_libs     = [] # libs to export from this target
        self.exported_syslibs  = [] # exported system libraries
        self.exported_assets: List[Asset] = [] # exported asset files
        self.papa_path = None # recorded path for previous papa deployment
        self.os_windows = System.windows
        self.os_linux   = System.linux
        self.os_macos   = System.macos
        self._set_args(args)
        self._update_platform_aliases()
        self.dep._update_dep_name_and_dirs(self.name)
        self.init()
        self._update_platform_aliases() # allow init() to redefine the platform


    def _update_platform_aliases(self):
        self.windows = self.config.windows
        self.linux   = self.config.linux
        self.macos   = self.config.macos
        self.ios     = self.config.ios
        self.android = self.config.android
        self.raspi   = self.config.raspi
        self.oclea   = self.config.oclea
        self.mips    = self.config.mips


    def _set_args(self, args: List[str]):
        if not isinstance(args, list):
            raise RuntimeError(f'BuildTarget {self.name} target args must be a list')
        for arg in args:
            if arg: self.args.append(arg)
        #console(f'Added args to {self.name}: {self.args}')


    def children(self) -> List[BuildDependency]:
        """ Get resolved child dependencies """
        return self.dep.get_children()


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
        if not subpath: return self.dep.src_dir
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
        if not subpath: return self.dep.build_dir
        return util.path_join(self.dep.build_dir, subpath)


    def set_artifactory_ftp(self, ftp_url, auth='store'):
        """
        Configures the remote Artifactory FTP URL where packages
        will be checked for download. If a package with correct commit hash
        exists, it will be used instead of building locally.

        If auth='store' then system's secure keyring is used to store
        the credentials. If authentication fails, then credentials are cleared.

        The username and password can be overriden by ENV variables
        `MAMA_ARTIFACTORY_USER` and `MAMA_ARTIFACTORY_PASS` for use in build systems
        ```
            def dependencies(self):
                self.config.set_artifactory_url('myserver.com', auth='store')
                self.config.set_artifactory_url('myserver.com', auth='prompt')
        ```
        NOTE: Currently only FTP is supported
        """
        if not self.dep.is_root:
            return
        self.config.set_artifactory_ftp(ftp_url=ftp_url, auth=auth)


    def add_local(self, name, source_dir, mamafile=None, always_build=False, args=[]) -> BuildDependency:
        """
        Add a local dependency. This can be a git submodule or just some local folder.
        which contains its own CMakeLists.txt.
        Optionally you can override the default 'mamafile.py' with your own.

        If the local dependency folder does not contain a `mamafile.py`, you will have to
        provide your own relative or absolute mamafile path.

        Optionally, you can set your local library to always build using `always_build=True`.
        This is useful when chaining together sub-projects that do not depend on each other.

        Additional arguments can be passed to the target mamafile. The target mamafile
        will have to check `self.args` for any arguments of interest.
        ```
        self.add_local('zlib', '3rdparty/zlib')
        self.add_local('zlib', '3rdparty/zlib', mamafile='mama/zlib.py')
        self.add_local('avdecoder', 'lib/avdecoder', always_build=True)
        ```
        """
        if self.dep.from_artifactory: # already loaded from artifactory?
            return self.get_dependency(name)
        return self.dep.add_child(LocalSource(name, source_dir, mamafile, always_build, args))


    def add_git(self, name, git_url, git_branch='', git_tag='', mamafile=None, shallow=True, args=[]) -> BuildDependency:
        """
        Add a remote GIT dependency.
        The dependency will be cloned and updated according to mamabuild.
        Use `mama update` to force update the git repositories.
    
        If the remote GIT repository does not contain a `mamafile.py`, you will have to
        provide your own relative or absolute mamafile path.

        For PUBLIC repositories, only use `https://` to prevent clone failures!!!

        Any arguments are passed onto child targets as `self.args`.
        ```
        self.add_git('ReCpp', 'git@github.com:RedFox20/ReCpp.git')
        self.add_git('ReCpp', 'git@github.com:RedFox20/ReCpp.git', git_branch='master')
        self.add_git('opencv', 'https://github.com/opencv/opencv.git', 
                     git_branch='3.4', mamafile='mama/opencv_cfg.py')
        ```
        """
        if self.dep.from_artifactory: # already loaded from artifactory?
            return self.get_dependency(name)
        return self.dep.add_child(Git(name, git_url, git_branch, git_tag, mamafile, shallow, args))


    def add_artifactory_pkg(self, name, version='latest', fullname=None) -> BuildDependency:
        """
        Adds an Artifactory only dependency.
        The dependency will be downloaded from the artifactory url.

        If the remote artifactory does not contain this package,
        an error is thrown during build.

        If a version value is given, mamabuild will try to automatically
        figure out the appropriate remote package.

        If a fullname value is given, only the specific artifactory package
        will be used as an override. This is mostly useful for source-only packages
        and for platform-specific configuration.

        ```
        self.add_artifactory_pkg('mylib', version='latest')
        self.add_artifactory_pkg('mylib', version='df76b66')
        self.add_artifactory_pkg('mylib', fullname='mylib-linux-x64-release-df76b66')
        ```
        """
        if self.dep.from_artifactory: # already loaded from artifactory?
            return self.get_dependency(name)
        return self.dep.add_child(ArtifactoryPkg(name, version=version, fullname=fullname))


    def get_dependency(self, name: str) -> BuildDependency:
        """
        Finds a child dependency by name.
        ```
            zlib_dep = self.get_dependency('zlib')
        ```
        """
        if self.dep.name == name:
            return self.dep
        for dep in self.children():
            if dep.name == name:
                return dep
        raise KeyError(f"BuildTarget {self.name} has no child dependency named '{name}'")


    def find_target(self, name, recursive=True):
        """
        Finds a child BuildTarget by name.
        ```
            zlib = self.find_target('zlib')
        ```
        """
        found = self._find_target(name, recursive=recursive)
        if not found:
            raise KeyError(f"BuildTarget {self.name} has no child target named '{name}'")
        return found


    def _find_target(self, name, recursive):
        if self.name == name:
            return self
        children = self.children()
        for dep in children:
            if dep.name == name:
                return dep.target
        if recursive: # now search the children's children
            for dep in children:
                target = dep.target._find_target(name, recursive=True)
                if target:
                    return target
        return None


    ## TODO: Move this into `package.py`
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
        Another example:
        ```
        def dependencies(self):
            self.add_git('curl', 'https://github.com/RedFox20/curl.git')
        def configure(self):
            # inject libcurl to us using 'CURL_INCLUDE_DIR' and 'CURL_LIBRARY'
            self.inject_products(self.name, 'curl', 'CURL_INCLUDE_DIR', 'CURL_LIBRARY')
        ```
        """
        dst_dep = self.get_dependency(dst_dep)
        src_dep = self.get_dependency(src_dep)
        dst_dep.product_sources.append( (src_dep, include_path, libs, libfilters) )


    ## TODO: Move this into `package.py`
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
        Gets target products as a tuple: (include_paths:str, libs:str)
        ```
            zlib_inc, zlib_libs = self.get_target_products('zlib')
            # zlib_inc  --> 'build/zlib/windows/include'
            # zlib_libs --> 'build/zlib/windows/RelWithDebInfo/zlibstatic.lib'
        ```
        """
        dep = self.get_dependency(target_name)
        target:BuildTarget = dep.target
        return (target._get_exported_includes(), target._get_exported_libs(None))


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
            dependency = util.normalized_join(self.build_dir(), dependency)
            self.build_products.append(dependency)
            #console(f'    {self.name}.build_products += {dependency}')


    def no_export_includes(self):
        """
        Declares that we do not have any includes to export. This is necesssary to prevent
        automatic includes generation.
        ```
            def package(self):
                self.no_export_includes()
                self.export_lib('mylib.dll')
        ```
        """
        self.no_includes = True


    def no_export_libs(self):
        """
        Declares that we do not have any libs to export. This is necesssary to prevent
        automatic lib search. This is most common for header-only libraries which might
        build some test binaries that would otherwise exported unnecessarily.
        ```
            def package(self):
                self.no_export_libs()
                self.export_include('include')
        ```
        """
        self.no_libs = True


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
        return package.export_include(self, include_path, build_dir=build_dir)


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
        return package.export_includes(self, include_paths, build_dir=build_dir)


    def export_lib(self, relative_path, src_dir=False, build_dir=True):
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
        if src_dir and build_dir:
            build_dir = False
        return package.export_lib(self, relative_path, build_dir=build_dir)


    def export_libs(self, path = '.', pattern_substrings = ['.lib', '.a'], src_dir=False, build_dir=True, order=None):
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
        if src_dir and build_dir:
            build_dir = False
        return package.export_libs(self, path, pattern_substrings, build_dir, order)


    def export_asset(self, asset, category=None, src_dir=True, build_dir=False):
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
        if not src_dir and not build_dir:
            build_dir = True
        return package.export_asset(self, asset, category, build_dir=build_dir)


    def export_assets(self, assets_path: str, pattern_substrings = [], category=None, src_dir=True, build_dir=False):
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
        if not src_dir and not build_dir:
            build_dir = True
        return package.export_assets(self, assets_path, pattern_substrings, category, build_dir=build_dir)


    def export_syslib(self, name: str, apt='', required=True):
        """
        For UNIX: Find and export system libraries so they are automatically linked with mamabuild.

        :returns: TRUE if syslib was exported; FALSE if required=False and syslib not found
        ```
            self.export_syslib('uuid')
            # will attempt to find system library in this order:
            #   1. uuid
            #   2. libuuid.so
            #   3. libuuid.a

            self.export_syslib('dw', 'libdw-dev')
            # upon failure, recommend user to install missing package from `apt install libdw-dev`
        ```
        """
        return package.export_syslib(self, name, apt, required)


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
        cmake.inject_env(self)


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


    def enable_from_env(self, name, enabled='ON', force=False):
        """
        Adds a CMake option if the environment variable `name` is set.
        ```
            self.enable_from_env('BUILD_TESTS')
        ```
        """
        env = os.getenv(name)
        if force or (env and (env == '1' or env == 'ON' or env == 'TRUE')):
            self.add_cmake_options(f'{name}={enabled}')


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


    def prefer_gcc(self):
        """ Configures the entire build chain to prefer GCC if possible """
        self.config.prefer_gcc(self.name)


    def prefer_clang(self):
        """ Configures the entire build chain to prefer Clang if possible """
        self.config.prefer_clang(self.name)


    def _get_cxx_std(self):
        return self.cmake_cxxflags.get('/std' if self.windows else '-std', '')

    def _set_cxx_std(self, std):
        self.cmake_cxxflags['/std' if self.windows else '-std'] = std


    def enable_cxx23(self):
        """ Enable C++23 standard """
        self._set_cxx_std('c++latest' if self.windows else 'c++2b')

    def is_enabled_cxx23(self):
        if 'CXX23' in self.args: return True
        std = self._get_cxx_std()
        return 'c++23' in std or 'c++2b' in std or 'c++latest' in std


    def enable_cxx20(self):
        """Enable C++20 standard"""
        if self.mips or self.raspi or self.oclea:
            self._set_cxx_std('c++2a') # older toolchains typically need c++2a
        else:
            self._set_cxx_std('c++20')

    def is_enabled_cxx20(self):
        if 'CXX20' in self.args: return True
        std = self._get_cxx_std()
        return 'c++20' in std or 'c++2a' in std


    def enable_cxx17(self):
        """Enable C++17 standard"""
        flag = 'c++17'
        if 'g++' in self.config.cxx_path and self.config.cxx_version:
            gcc_major = int(self.config.cxx_version.split('.')[0])
            if gcc_major < 8: flag = 'c++1z' # older toolchains typically need c++1z
        self._set_cxx_std(flag)

    def is_enabled_cxx17(self):
        if 'CXX17' in self.args: return True
        std = self._get_cxx_std()
        return 'c++17' in std or 'c++1z' in std


    def enable_cxx14(self):
        """Enable C++14 standard"""
        self._set_cxx_std('c++14')

    def is_enabled_cxx14(self):
        if 'CXX14' in self.args: return True
        std = self._get_cxx_std()
        return 'c++14' in std


    def enable_cxx11(self):
        """Enable C++11 standard"""
        self._set_cxx_std('c++11')

    def is_enabled_cxx11(self):
        if 'CXX11' in self.args: return True
        std = self._get_cxx_std()
        return 'c++11' in std


    def copy(self, src: str, dst: str, filter: list = None):
        """
        Utility for copying files and folders
        ```
            # copies built .so into an android archive
            self.copy(self.build_dir('libAwesome.so'), 
                      self.source_dir('deploy/Awesome.aar/jni/armeabi-v7a'))
        ```
        - filter: can be a string or list of strings to filter files by suffix
                  example: filter=['.h'] or filter='.hpp'
        """
        if util.copy_if_needed(src, dst, filter):
            if self.config.verbose: console(f'copy {src} --> {dst}')


    def copy_built_file(self, builtFile: str, copyToFolder: str):
        """
        Utility for copying files within the build directory.
        ```
            self.copy_built_file('RelWithDebInfo/libawesome.a', 'lib')
        ```
        """
        src = f'{self.build_dir()}/{builtFile}'
        dst = f'{self.build_dir()}/{copyToFolder}/{os.path.basename(builtFile)}'
        if not os.path.exists(src) and os.path.exists(dst):
            return # src is missing, but dst exists, ignore error
        if util.copy_if_needed(src, dst):
            if self.config.verbose: console(f'copy_built_file {src} --> {dst}')


    def copy_deployed_folder(self, src_dir: str, dst_dir: str, filter: list = None):
        """
        Utility for copying folders from source dir.
        ```
            self.copy_deployed_folder('deploy/NanoMesh', 'C:/Projects/Game/Plugins')
            # --> 'C:/Projects/Game/Plugins/NanoMesh
        ```
        """
        src = self.source_dir(src_dir)
        dst = dst_dir
        if util.copy_if_needed(src, dst, filter):
            if self.config.verbose: console(f'copy_deployed_folder {src} --> {dst}')


    def download_file(self, remote_url: str, local_dir: str, force=False):
        """
        Downloads a file if it doesn't already exist.
        ```
            self.download_file('http://example.com/file1', 'bin')
            # --> 'bin/file1'
        ```
        """
        return util.download_file(remote_url, local_dir, force)


    def download_and_unzip(self, remote_zip: str, extract_dir: str, unless_file_exists=None):
        """
        Downloads and unzips an archive if it doesn't already exist.

        unless_file_exists -- If the specified file exists, then download and unzip steps are skipped.
        ```
            self.download_and_unzip('http://example.com/archive.zip', 
                                    'bin', 'bin/unzipped_file.txt')
            # --> 'bin/'  on success
            # --> None    on failure
        ```
        """
        return util.download_and_unzip(remote_zip, extract_dir, unless_file_exists)


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
        Call this to completely skip the build step every time
        ```
            def dependencies(self):
                self.nothing_to_build()
        ```
        """
        self.dep.nothing_to_build = True
        self.dep.should_rebuild = False


    def gnu_project(self, name:str, version:str,
                    url:str='',
                    git:str='',
                    build_products=[],
                    autogen=False,
                    configure='configure'):
        """
        Creates a new GnuProject instance for building GNU projects from source.
        - name: name of the project, eg 'gmp'
        - version: version of the project, eg '6.2.1'
        - build_products: the final products to build, eg [BuildProduct('{{installed}}/lib/libgmp.a', 'mypath/libgmp.a')].
                          Supported project variables {{installed}}, {{source}}, {{build}}
        - url: url to download the project, eg 'https://gmplib.org/download/gmp/{{project}}.tar.xz'
        - git: git to clone the project from
        - autogen: whether to use ./autogen.sh before running ./configure
        - configure: the configuration command, by default 'configure' but can be 'make config' etc
        ```
            gmp = self.gnu_project('gmp', '6.2.1', 'https://gmplib.org/download/gmp/{{project}}.tar.xz', 'lib/libgmp.a')
            gmp.configure()
        ```
        """
        return GnuProject(self, name, version, url=url, git=git, build_products=build_products,
                          autogen=autogen, configure=configure)


    def get_cc_prefix(self):
        """
        Useful for crosscompiling builds, returns the prefix of the compiler, eg '/usr/bin/mipsel-linux-gnu-'
        """
        cc = self.config.get_preferred_compiler_paths()[0]
        filename = os.path.basename(cc)
        if filename.endswith('gcc'):
            filename = filename[:-3]
        else:
            return None # there is no prefix it's something like /usr/bin/gcc-11
        return os.path.join(os.path.dirname(cc), filename)


    def run(self, command: str, src_dir=False, exit_on_fail=True):
        """
        Run a command in the build or source folder.
        Can be used for any custom commands or custom build systems.
        src_dir -- [False] If true, then command is relative to source directory.
        ```
            self.run('./configure', src_dir=True)
            self.run('make release -j7') # run in build dir
        ```
        """
        run_in_project_dir(self, command, src_dir, exit_on_fail)


    def run_program(self, working_dir: str, command: str, exit_on_fail=True, env=None):
        """
        Run any program in any directory. Can be used for custom tools.
        ```
            self.run_program(self.source_dir('bin'), 
                             self.source_dir('bin/DbTool'))
        ```
        """
        run_in_working_dir(self, working_dir, command, exit_on_fail=exit_on_fail, env=env)


    def run_with_gdb(self, command: str, args: str, src_dir=True, gdb_by_default=True):
        """
        Run a program with gdb if requested, otherwise run normally.
        To control this, add 'gdb' or 'nogdb' to args.
        The parameter `gdb` controls what the default behavior is.
        If used inside start(), then `mama start=nogdb` or `mama start=gdb` will control GDB enablement
        """
        args, gdb = filter_gdb_arg(args, gdb_by_default)
        if gdb:
            run_gdb(self, f'{command} {args}', src_dir=src_dir)
        else:
            run_in_command_dir(self, f'{command} {args}', src_dir=src_dir)


    def gdb(self, command: str, src_dir=True):
        """
        Run a command with gdb in the build folder.
        ```
            self.gdb('bin/NanoMeshTests')
        ```
        """
        return run_gdb(self, command, src_dir)


    def gtest(self, executable: str, args: str, src_dir=True, gdb=False):
        """
        Runs a gtest executable with gdb by default.
        The gtest report is written to $src_dir/test/report.xml.
        Arguments
        - executable -- which executable to run
        - args -- a string of options separated by spaces, 
          'gdb', 'nogdb' or gtest fixture/test partial name
        - src_dir -- [True] If true, then executable is relative to source directory.
        - gdb -- [False] If true, then run with gdb.
        ```
            self.gtest("bin/MyAppGtests", "nogdb", src_dir=True)
            self.gtest("bin/MyAppGtests", "MyFixtureName.TheTestName", src_dir=True)
        ```
        """
        run_gtest(self, executable, args=args, src_dir=src_dir, gdb=gdb)


    ########## Customization Points ###########


    def init(self):
        """
        Perform any initialization steps right after the mamafile is loaded.
        ```
        class MyProject(mama.BuildTarget):
            def init(self):
                self.version = '1.2.3'
        """
        pass


    def settings(self):
        """
        Define any settings at this stage, it is always
        the first step after git clone or loading from artifactory.

        ```
        class MyProject(mama.BuildTarget):
            def settings(self):
                # only valid for root targets
                self.set_artifactory_ftp('artifacts.myftp.com', auth='store')
                self.nothing_to_build()
        """
        pass


    def dependencies(self):
        """
        Add any additional dependencies in this step,
        or setup project configuration for root targets.

        If this target is fetched as a package from the artifactory,
        then any add_git()/add_local() calls will be ignored.
        ```
        class MyRootProject(mama.BuildTarget):
            def dependencies(self):
                # only valid for root targets
                self.set_artifactory_ftp('artifacts.myftp.com', auth='store')

                self.add_git('ReCpp', 'http://github.com/RedFox20/ReCpp.git')
                self.add_local('fbxsdk', 'third_party/FBX')
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
        if self.no_includes: self.default_package_includes()
        if self.no_libs: self.default_package_libs()


    ## TODO: move this into `package.py`
    def default_package_includes(self):
        """
        Performs default INCLUDE packaging steps.
        It can also be called manually to collect includes.
        ```
        def package(self):
            self.default_package_includes()
        ```
        """
        # try multiple common/popular C and C++ library include patterns
        if   self.export_include('include', build_dir=True):  pass
        elif self.export_include('include', build_dir=False): pass
        elif self.export_include('src',     build_dir=False): pass
        elif self.export_include('',        build_dir=False): pass


    ## TODO: move this into `package.py`
    def default_package_libs(self):
        """
        Performs default LIB packaging steps.
        It can also be called manually to collect libs.
        ```
        def package(self):
            self.default_package_libs()
        ```
        """
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
        Or:
        ```
        def deploy(self):
            self.default_deploy()
        ```
        """
        self.default_deploy()


    def default_deploy(self):
        self.papa_deploy(f'deploy/{self.name}', src_dir=False)


    def papa_deploy(self, package_path, src_dir=False,
                    r_includes=False, r_dylibs=False,
                    r_syslibs=False, r_assets=False):
        """
        This will create a PAPA package, which includes
            package_path/papa.txt
            package_path/{includes}
            package_path/{libs}
            package_path/{assets}

        src_dir -- Whether package will be deployed to src dir or build dir
        r_includes -- Whether to recursively export includes from dynamic libaries
        r_dylibs   -- Whether to recursively export all *.dll *.so *.dylib libraries
        r_syslibs  -- Whether to include system libraries from child dependencies
        r_assets   -- Whether to include assets from child dependencies

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
        if self.config.list:
            return # don't deploy during listing
        build_dir = not src_dir
        self.papa_path = package.target_root_path(self, package_path, build_dir=build_dir)
        papa_deploy_to(self, self.papa_path, \
            r_includes=r_includes, r_dylibs=r_dylibs, \
            r_syslibs=r_syslibs, r_assets=r_assets)


    def test(self, args):
        """
        Perform test steps here with test args.
        `mama test arg1 arg2 arg3`
        ```
            def test(self, args):
                # simply runs an executable with GDB
                self.gdb(f'RppTests {args}')
                # or run gtest executable, with GDB by default
                # or you can provide `nogdb` argument to disable GDB
                self.gtest(f'bin/project_gtests', args, src_dir=True)
        ```
        """
        pass


    def start(self, args):
        """
        Start a custom process through mama
        `mama target start=arg`
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
        cmake.run_build(self, install=True)


    def clean_target(self):
        self.dep.clean()


    def cmake_build(self):
        if self.config.print:
            console('\n\n#############################################################')
            console(f"CMakeBuild {self.name} ({self.cmake_build_type})")
        config_start = time.time()
        self.dep.ensure_cmakelists_exists()
        cmake.inject_env(self)
        cmake.run_config(self) # THROWS on CMAKE failure
        config_stop = time.time()
        build_start = config_stop
        cmake.run_build(self, install=True) # THROWS on CMAKE failure
        build_stop = time.time()
        if self.config.print:
            e_config = util.get_time_str(config_stop - config_start)
            e_build = util.get_time_str(build_stop - build_start)
            e_total = util.get_time_str(build_stop - config_start)
            console(f"CMakeBuild {self.name} ({self.cmake_build_type}) config {e_config} build {e_build} total {e_total}", color='green')


    def is_test_target(self):
        """
        TRUE if this build target was specified along with `test` command.
        This matches `all`, specific cmdline targets, and the `root` target.
        ```
        mama test              # the root target
        mama test this_target  # specific target
        mama test all          # all targets
        ```
        """
        if not self.config.test:
            return False
        # `mama test` --> only test root target
        if self.config.no_target() and self.dep.is_root:
            return True
        # `mama test ReCpp` --> only test current target
        # `mama test all` --> current target matches all
        return self.config.has_target() and self.dep.is_current_target()


    def is_current_target(self):
        """
        TRUE if this BuildTarget is a configuration target for
        build/test/etc. This matches 'all' or specific cmdline targets:
        ```
            mama build
            mama build this_target
        ```
        """
        return self.dep.is_current_target()


    ## Build only this target
    def _execute_tasks(self):
        if self.dep.already_executed:
            return
        try:
            self.dep.already_executed = True
            self._execute_build_tasks()
            self._execute_deploy_tasks()
            self._execute_run_tasks()
        except Exception as err:
            import traceback
            console(f'  [BUILD FAILED]  {self.dep.name}  \n{err}\n\n')
            traceback.print_exc()
            exit(-1) # exit without further stack trace


    def try_automatic_artifactory_fetch(self):
        if not self.dep.can_fetch_artifactory(print=True, which='AUTO'):
            return None

        # auto-fetch if:
        # - is not the root project, roots should never be fetched
        # - not a deploy/upload task
        # - not the current build target eg `mama build this_target`
        is_target = not self.dep.is_root and self.is_current_target()
        is_deploy = self.config.deploy or self.config.upload
        is_build = self.config.build
        if is_target and not is_deploy and not is_build:
            fetched, _ = artifactory_fetch_and_reconfigure(self) # this will reconfigure packaging
            return fetched
        return None


    ## TODO: move all of this into a new utility
    def _execute_build_tasks(self):
        can_build = not self.dep.nothing_to_build
        if can_build and self.dep.should_rebuild and not self.dep.from_artifactory:
            self.configure() # user customization
            if can_build:
                fetched = self.try_automatic_artifactory_fetch()
                if not fetched:
                    self.build() # user build customization

                self.dep.successful_build()
                if not fetched:
                    package.clean_intermediate_files(self)

        # skip package() if we already fetched it as a package from artifactory()
        if not self.dep.from_artifactory:
            self.package() # user customization

            # no packaging provided by user; use default packaging instead
            if not self.exported_includes and not self.no_includes:
                self.default_package_includes()
            if not (self.exported_libs or self.exported_syslibs) and not self.no_libs:
                self.default_package_libs()

        # only save and print exports if we built anything
        if self.dep.build_dir_exists():
            self.dep.save_exports_as_dependencies(self.exported_libs)
            # print exports only if target match
            if self.is_current_target():
                self.print_exports()


    def _execute_deploy_tasks(self):
        if not self.config.deploy and not self.config.upload:
            return

        no_targets = self.config.no_target() and self.dep.is_root # only root target
        for_all = self.config.targets_all() # all targets
        one_target = not for_all and self.is_current_target() # only one target
        if not (for_all or no_targets or one_target):
            return # not going to deploy

        self.deploy() # user customization

        if self.config.upload:
            papa_upload_to(self, self.papa_path)


    def _execute_run_tasks(self):
        if self.is_test_target():
            test_args = self.config.test.lstrip()
            if self.config.print: console(f'  - Testing {self.name} {test_args}')
            self.test(test_args)

        if self.config.start:
            # start only if it's the current target or root target
            if self.is_current_target() or (self.dep.is_root and self.config.no_specific_target()):
                start_args = self.config.start.lstrip()
                if self.config.print: console(f'  - Starting {self.name} {start_args}')
                self.start(start_args)


    def _print_ws_path(self, what, path, abs_path, check_exists=True):
        def exists():
            return '' if os.path.exists(path) else '   !! (path does not exist) !!'
        if path.startswith('-framework'):
            console(f'    {what}  {path}')
        elif not abs_path and path.startswith(self.config.workspaces_root):
            console(f'    {what}  {path[len(self.config.workspaces_root) + 1:]}{exists()}')
        elif not abs_path and path.startswith(self.source_dir()):
            console(f'    {what}  {path[len(self.source_dir()) + 1:]}{exists()}')
        elif not abs_path and path.startswith(self.build_dir()):
            console(f'    {what}  {path[len(self.build_dir()) + 1:]}{exists()}')
        else:
            ex = exists() if check_exists else ''
            console(f'    {what}  {path}{ex}')


    def print_exports(self, abs_paths=False):
        if not self.config.print:
            return
        if not (self.exported_includes or self.exported_libs or self.exported_syslibs or self.exported_assets):
            return

        console(f'  - Package {self.name}')
        for include in self.exported_includes: self._print_ws_path('<I>', include, abs_paths)
        for library in self.exported_libs:     self._print_ws_path('[L]', library, abs_paths)
        for library in self.exported_syslibs:  self._print_ws_path('[S]', library, abs_paths, check_exists=False)
        if self.config.deploy or self.config.upload:
            for asset in self.exported_assets: self._print_ws_path('[A]', asset.srcpath, abs_paths, check_exists=False)
        elif self.exported_assets:
            assets = 'assets' if len(self.exported_assets) > 1 else 'asset'
            console(f'    [A]  ({len(self.exported_assets)} {assets})')


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
        if self.config.print:
            console('\n#########################################')
            console(f'MSBuild {self.name} {projectfile}')
        msbuild.msbuild_build(self.config, self.source_dir(projectfile), properties)


######################################################################################
        