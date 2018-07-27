import os.path, shutil
import pathlib, stat, time, subprocess, concurrent.futures
from mama.system import System, console, execute, execute_echo
from mama.util import save_file_if_contents_changed, glob_with_name_match, \
                      normalized_path, write_text_to
from mama.build_dependency import BuildDependency, Git
from mama.build_config import BuildConfig
from mama.cmake_configure import run_cmake_config, run_cmake_build, cmake_default_options, \
                                 cmake_inject_env, cmake_buildsys_flags, cmake_generator
import mama.util as util

######################################################################################

##
# Describes a single configurable build target
# This is the main public interface for configuring a specific target
# For project-wide configuration, @see BuildConfig in self.config
#
# Customization points:
#
# class MyProject(mama.BuildTarget):
#     local_workspace = 'build'
#     
#
class BuildTarget:


    def __init__(self, name, config:BuildConfig, dep:BuildDependency, args:list):
        if config is None: raise RuntimeError(f'BuildTarget {name} config argument must be set')
        if dep is None:    raise RuntimeError(f'BuildTarget {name} dep argument must be set')
        self.config = config
        self.name = name
        self.dep  = dep
        self.args = [] # user defined args for this target (must be a list)
        self.install_target   = 'install'
        self.cmake_ndk_toolchain = f'{config.android_ndk_path}/build/cmake/android.toolchain.cmake' if config.android_ndk_path else ''
        self.cmake_ios_toolchain = ''
        self.cmake_opts       = []
        self.cmake_cxxflags   = dict()
        self.cmake_ldflags    = dict()
        self.cmake_build_type = 'Debug' if config.debug else 'RelWithDebInfo'
        self.enable_exceptions = True
        self.enable_unix_make  = False
        self.enable_ninja_build = True and config.ninja_path # attempt to use Ninja
        self.enable_multiprocess_build = True
        self.build_dependencies = [] # dependency files
        self.exported_includes = [] # include folders to export from this target
        self.exported_libs     = [] # libs to export from this target
        self.windows = self.config.windows # convenient alias
        self.linux   = self.config.linux
        self.macos   = self.config.macos
        self.ios     = self.config.ios
        self.android = self.config.android
        self.set_args(args)


    def set_args(self, args:list):
        if not isinstance(args, list):
            raise RuntimeError(f'BuildTarget {self.name} target args must be a list')
        self.args += args
        #console(f'Added args to {self.name}: {self.args}')


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


    ##
    #  Add a local dependency. This can be a git submodule or just some local folder
    #  which contains its own CMakeLists.txt
    #  Optionally you can override the default 'mamafile.py' with your own.
    #  
    #  If the local dependency folder does not contain a `mamafile.py`, you will have to
    #  provide your own relative or absolute mamafile path.
    # Ex:
    #   self.add_local('zlib', '3rdparty/zlib')
    #   self.add_local('zlib', '3rdparty/zlib', mamafile='mama/zlib.py')
    #
    def add_local(self, name, source_dir, mamafile=None, args=[]):
        src      = self._get_full_path(source_dir)
        mamafile = self._get_mamafile_path(name, mamafile)
        dependency = BuildDependency.get(name, self.config, BuildTarget, \
                        workspace=self.dep.workspace, src=src, mamafile=mamafile, args=args)
        self.dep.children.append(dependency)


    ##
    #  Add a remote GIT dependency
    #  The dependency will be cloned and updated according to mamabuild.
    #  Use `mama update` to force update the git repositories.
    #
    #  If the remote GIT repository does not contain a `mamafile.py`, you will have to
    #  provide your own relative or absolute mamafile path.
    #
    #  Any arguments are passed onto child targets as `self.args`
    # Ex:
    #   self.add_git('ReCpp', 'https://github.com/RedFox20/ReCpp.git')
    #   self.add_git('ReCpp', 'https://github.com/RedFox20/ReCpp.git', git_branch='master')
    #   self.add_git('opencv', 'https://github.com/opencv/opencv.git', git_branch='3.4', mamafile='mama/opencv_cfg.py')
    #
    def add_git(self, name, git_url, git_branch='', git_tag='', mamafile=None, args=[]):
        git = Git(git_url, git_branch, git_tag)
        mamafile = self._get_mamafile_path(name, mamafile)
        dependency = BuildDependency.get(name, self.config, BuildTarget, \
                        workspace=self.dep.workspace, git=git, mamafile=mamafile, args=args)
        self.dep.children.append(dependency)

    ##
    # Finds a dependency by name
    #
    def get_dependency(self, name):
        for dep in self.dep.children:
            if dep.name == name:
                return dep
        raise KeyError(f"BuildTarget {self.name} has no child dependency named '{name}'")


    ##
    #  Injects products from `src_dep` into `dst_dep` as CMake defines
    #  Name of defines is given via `include_path` and `libs` params
    #  `libfilters` does simple string matching; if nothing matches, the first export lib is chosen
    # Ex:
    #    self.inject_products('libpng', 'zlib', 'ZLIB_INCLUDE_DIR', 'ZLIB_LIBRARY', 'zlibstatic')
    #
    def inject_products(self, dst_dep, src_dep, include_path, libs, libfilters=None):
        dst_dep = self.get_dependency(dst_dep)
        src_dep = self.get_dependency(src_dep)
        dst_dep.depends_on.append(src_dep)
        dst_dep.product_sources.append( (src_dep, include_path, libs, libfilters) )

    ##
    #  Collects all results injected by `inject_products`
    #  Returns a list of injected defines:
    #  [ 'ZLIB_INCLUDE_DIR=path/to/zlib/include', 'ZLIB_LIBRARY=path/to/lib/zlib.a', ... ]
    #
    def get_product_defines(self):
        defines = []
        for source in self.dep.product_sources:
            srcdep = source[0]
            includes = srcdep.target.get_exported_includes()
            libraries = srcdep.target.get_exported_libs(source[3])
            #console(f'grabbing products: {srcdep.name}; includes={includes}; libraries={libraries}')
            defines.append(f'{source[1]}={includes}')
            defines.append(f'{source[2]}={libraries}')
        return defines

    def get_exported_includes(self):
        return ';'.join(self.exported_includes) if self.exported_includes else ''

    def get_exported_libs(self, libfilters):
        #console(f'libfilters={libfilters}')
        if not self.exported_libs: return
        if not libfilters: return ';'.join(self.exported_libs)
        libs = []
        for lib in self.exported_libs:
            if libfilters in lib: libs.append(lib)
        if not libs: libs.append(self.exported_libs[0])
        return ';'.join(libs)


    ##
    # Gets target products as a tuple: (include_paths=[], libs=[])
    # Ex:
    #  self.get_target_products('zlib')
    def get_target_products(self, target_name):
        dep = self.get_dependency(target_name)
        target:BuildTarget = dep.target
        return (target.get_exported_includes(), target.get_exported_libs())


    ## 
    # Manually add a build dependency to prevent unnecessary rebuilds
    # Normally the build dependency is detected from the packaged libraries.
    # 
    # if the dependency file does not exist, then the project will be rebuilt
    #
    # if your project has no build dependencies, it will always be rebuilt, so make sure
    # to add_build_dependency or export_lib
    #
    def add_build_dependency(self, all=None, windows=None, linux=None, macos=None, ios=None, android=None):
        dependency = all if all else self.select(windows, linux, macos, ios, android)
        if dependency:
            dependency = normalized_path(os.path.join(self.dep.build_dir, dependency))
            self.build_dependencies.append(dependency)
            #console(f'    {self.name}.build_dependencies += {dependency}')


    ##
    # CUSTOM PACKAGE INCLUDES (if self.default_package() is insufficient)
    # 
    # Export include path relative to source directory
    #  OR if build_dir=True, then relative to build directory
    # Ex:
    #   self.export_include('./include') # MyRepo/include
    #   self.export_include('installed/MyLib/include', build_dir=True) # CMake installed includes in build dir
    #
    def export_include(self, include_path, build_dir=False):
        root = self.dep.build_dir if build_dir else self.dep.src_dir
        include_path = normalized_path(os.path.join(root, include_path))
        #console(f'export_include={include_path}')
        if os.path.exists(include_path):
            if not include_path in self.exported_includes:
                self.exported_includes.append(include_path)
            return True
        return False

    ##
    # CUSTOM PACKAGE INCLUDES (if self.default_package() is insufficient)
    #
    # Export include paths relative to source directory
    #  OR if build_dir=True, then relative to build directory
    # Ex:
    #   self.export_includes(['include', 'src/moreincludes'])
    #   self.export_includes(['installed/include', 'installed/src/moreincludes'], build_dir=True)
    def export_includes(self, include_paths=[''], build_dir=False):
        self.exported_includes = []
        for include_path in include_paths:
            self.export_include(include_path, build_dir)


    ##
    # CUSTOM PACKAGE LIBS (if self.default_package() is insufficient)
    #
    # Export a specific lib relative to build directory
    #  OR if src_dir=True, then relative to source directory
    # Ex:
    #   self.export_lib('mylib.a')  # from build dir
    #   self.export_lib('lib/mylib.a', src_dir=True)  # from project source dir
    def export_lib(self, relative_path, src_dir=False):
        root = self.dep.src_dir if src_dir else self.dep.build_dir
        path = normalized_path(os.path.join(root, relative_path))
        if os.path.exists(path):
            self.exported_libs.append(path)
            self._remove_duplicate_export_libs()


    ##
    # CUSTOM PACKAGE LIBS (if self.default_package() is insufficient)
    #
    # Export several libs relative to build directory using EXTENSION MATCHING
    #  OR if src_dir=True, then relative to source directory
    #
    # Ex:
    #   self.export_libs()  # gather any .lib or .a from build dir
    #   self.export_libs('.', ['.dll', '.so'])   # gather any .dll or .so from build dir
    #   self.export_libs('lib', src_dir=True)  # export everything from project/lib directory
    #   self.export_libs('external/lib')  # gather specific static libs from build dir
    #   
    #   # export the libs in a particular order for Linux linker
    #   self.export_libs('lib', order=[
    #       'xphoto', 'calib3d', 'flann', 'core'
    #   ])
    #   -->  [..others.., libopencv_xphoto.a, libopencv_calib3d.a, libopencv_flann.a, libopencv_core.a]
    # 
    def export_libs(self, path = '.', pattern_substrings = ['.lib', '.a'], src_dir=False, order=None):
        root = self.dep.src_dir if src_dir else self.dep.build_dir
        path = os.path.join(root, path)
        libs = glob_with_name_match(path, pattern_substrings)
        if order:
            def lib_index(lib):
                for i in range(len(order)):
                    if order[i] in lib: return i
                return -1
            def sort_key(lib):
                return lib_index(lib)
            libs.sort(key=sort_key)
        self.exported_libs += libs
        self._remove_duplicate_export_libs()
        return len(self.exported_libs) > 0


    def _remove_duplicate_export_libs(self):
        unique = dict()
        for lib in self.exported_libs:
            unique[os.path.basename(lib)] = lib
        self.exported_libs = list(unique.values())


    ##
    # Injects default platform and target specific environment variables
    # This can be used for custom build step
    # 
    def inject_env(self):
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

    ##
    #  Adds C / C++ flags for compilation step
    #  Supports many different usages: strings, list of strings, kwargs, or space separate string
    # Ex:
    #   self.add_cxx_flags('-Wall')
    #   self.add_cxx_flags(['-Wall', '-std=c++17'])
    #   self.add_cxx_flags('-Wall', '-std=c++17')
    #   self.add_cxx_flags('-Wall -std=c++17')
    def add_cxx_flags(self, *flags):
        for flag in flags:
            if isinstance(flag, list): self.add_cxx_flags(*flag)
            else: self._add_dict_flag(self.cmake_cxxflags, flag)
    
    ##
    #  Adds flags for linker step; No platform checking is done
    #  Supports many different usages: strings, list of strings, kwargs, or space separate string
    # Ex:
    #   self.add_ld_flags('-rdynamic')
    #   self.add_ld_flags(['-rdynamic', '-s'])
    #   self.add_ld_flags('-rdynamic', '-s')
    #   self.add_ld_flags('-rdynamic -s')
    def add_ld_flags(self, *flags):
        for flag in flags:
            if isinstance(flag, list): self.add_ld_flags(*flag)
            else: self._add_dict_flag(self.cmake_ldflags, flag)

    ##
    #  Adds C / C++ flags flags depending on configuration platform
    #  Supports many different usages: strings, list of strings, kwargs, or space separate string
    # Ex:
    #   self.add_cxx_flags('-Wall')
    #   self.add_cxx_flags(['-Wall', '-std=c++17'])
    #   self.add_cxx_flags('-Wall', '-std=c++17')
    #   self.add_cxx_flags('-Wall -std=c++17')
    #
    def add_platform_cxx_flags(self, windows=None, linux=None, macos=None, ios=None, android=None):
        flags = self.select(windows, linux, macos, ios, android)
        if flags: self.add_cxx_flags(flags)

    ##
    #  Adds linker flags depending on configuration platform
    #  Supports many different usages: strings, list of strings, or space separate string
    # Ex:
    #   self.add_platform_ld_flags(windows='/LTCG', ios=['-lobjc', '-rdynamic'], linux='-rdynamic -s')
    #
    def add_platform_ld_flags(self, windows=None, linux=None, macos=None, ios=None, android=None):
        flags = self.select(windows, linux, macos, ios, android)
        if flags: self.add_ld_flags(flags)


    ## 
    #  Main method for configuring CMake options
    # Ex:
    #   self.add_cmake_options('ZLIB_STATIC=TRUE', 'NO_GUI=1')
    #   self.add_cmake_options(['ZLIB_STATIC=TRUE', 'NO_GUI=1'])
    #
    def add_cmake_options(self, *options):
        for option in options:
            if isinstance(option, list): self.cmake_opts += option
            else:                        self.cmake_opts.append(option)

    
    ##
    #  Selectively applies CMake options depending on configuration platform
    # Ex:
    #  self.add_platform_options(windows='ZLIB_STATIC=TRUE')
    #
    def add_platform_options(self, windows=None, linux=None, macos=None, ios=None, android=None):
        defines = self.select(windows, linux, macos, ios, android)
        if defines: self.cmake_opts += defines


    def select(self, windows, linux, macos, ios, android):
        if   self.config.windows and windows: return windows
        elif self.config.linux   and linux:   return linux
        elif self.config.macos   and macos:   return macos
        elif self.config.ios     and ios:     return ios
        elif self.config.android and android: return android
        return None

    ##
    # Enable a specific C++ standard
    def enable_cxx20(self):
        self.cmake_cxxflags['/std' if self.config.windows else '-std'] = 'c++latest' if self.config.windows else 'c++2a'
    def enable_cxx17(self):
        self.cmake_cxxflags['/std' if self.config.windows else '-std'] = 'c++17'
    def enable_cxx14(self):
        self.cmake_cxxflags['/std' if self.config.windows else '-std'] = 'c++14'
    def enable_cxx11(self):
        self.cmake_cxxflags['/std' if self.config.windows else '-std'] = 'c++11'


    ##
    # Utility for copying files within the build directory
    #
    def copy_built_file(self, builtFile, copyToFolder):
        src = f'{self.dep.build_dir}/{builtFile}'
        dst = f'{self.dep.build_dir}/{copyToFolder}'
        shutil.copy(src, dst)


    def download_file(self, remote_url, local_dir, force=False):
        util.download_file(remote_url, local_dir, force)


    def download_and_unzip(self, remote_zip, extract_dir):
        util.download_and_unzip(remote_zip, extract_dir)
        

    ##
    # Call this to completely skip the build step every time
    # and instead 
    def nothing_to_build(self):
        self.dep.nothing_to_build = True
        self.dep.should_rebuild = False


    # Run a command in the build folder. Can be used for any custom commands or custom build systems
    # Ex:
    #  self.run('./configure')
    #  self.run('make release -j7')
    #
    def run(self, command):
        execute(f'cd {self.dep.build_dir} && {command}', echo=True)

    
    # Run a command with gdb in the build folder
    def gdb(self, command, src_dir=False):
        if self.config.android or self.config.ios:
            return # nothing to run
        
        split = command.split(' ', 1)
        cmd = split[0].lstrip('.')
        args = split[1] if len(split) >= 2 else ''
        path = self.dep.src_dir if src_dir else self.dep.build_dir
        path = f"{path}/{os.path.dirname(cmd).lstrip('/')}"
        exe = os.path.basename(cmd)

        if self.config.windows:
            if not src_dir: path = f'{path}/{self.cmake_build_type}'
            gdb = exe
        else: # linux, macos
            gdb = f'gdb -batch -return-child-result -ex=r -ex=bt --args ./{exe} {args}'
        execute_echo(path, gdb)

    ########## Customization Points ###########


    ###
    # Add any dependencies in this step
    #   self.add_local(...)
    #   self.add_remote(...)
    #
    def dependencies(self):
        pass


    ###
    # Perform any pre-build steps here
    def configure(self):
        pass


    ###
    # Build this target. By default it uses CMake build
    def build(self):
        self.cmake_build()


    ### 
    # Perform any pre-clean steps here
    def clean(self):
        pass


    ###
    # Perform custom install steps here. By default it uses CMake install
    def install(self):
        self.cmake_install()


    ###
    # Perform any post-build steps to package the products.
    # If no headers or libs are exported, then `default_package()` will be run instead
    #
    # Every library should at least export some headers
    # Ex:
    #   def package(self):
    #       self.export_libs('.', ['.lib', '.a']) # export any .lib or .a from build folder
    #       self.export_includes(['AGL']) # export AGL as include from source folder
    #
    def package(self):
        pass
    

    def default_package(self):
        # try multiple common/popular C and C++ library include patterns
        if   self.export_include('include', build_dir=True):  pass
        elif self.export_include('include', build_dir=False): pass
        elif self.export_include('src',     build_dir=False): pass
        elif self.export_include('',        build_dir=False): pass

        # default export from {build_dir}/{cmake_build_type}
        if self.export_libs(self.cmake_build_type, src_dir=False): pass
        elif self.export_libs('lib', src_dir=False): pass
    

    ###
    # Perform test steps here with test args
    # `mama test arg1 arg2 arg3`
    # Ex:
    #   def test(self, args): 
    #      self.gdb(f'./RppTests {args}')
    #
    def test(self, args):
        pass



    ############################################


    def cmake_install(self):
        console('\n\n#############################################################')
        console(f"CMake install {self.name} ...")
        run_cmake_build(self)


    def clean_target(self):
        self.dep.clean()


    def cmake_build(self):
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
        run_cmake_build(self, cmake_buildsys_flags(self))
        self.dep.save_git_status()

    def is_test_target(self):
        return self.config.test and self.dep.is_root_or_config_target()

    ## Build only this target
    def execute_tasks(self):
        if self.dep.already_executed:
            return
        
        try:
            self.dep.already_executed = True

            if self.dep.should_rebuild and not self.dep.nothing_to_build:
                self.configure() # user customization
                if not self.dep.nothing_to_build:
                    self.build() # user customization
        
            self.package() # user customization

            # no packaging provided by user; use default packaging instead
            if not self.exported_includes or not self.exported_libs:
                self.default_package()

            self.dep.save_exports_as_dependencies(self.exported_libs)

            self.print_exports()

            if self.is_test_target():
                test_args = self.config.test.lstrip()
                console(f'  - Testing {self.name} {test_args}')
                self.test(test_args)
        except:
            console(f'  [BUILD FAILED]  {self.dep.name}')
            raise


    def _print_ws_path(self, what, path):
        n = len(self.config.workspaces_root) + 1
        exists = '' if os.path.exists(path) else '   !! (path does not exist) !!' 
        console(f'    {what}  {path[n:]}{exists}')

    ##
    # Prints out all the exported products
    def print_exports(self):
        console(f'  - Package {self.name}')
        for include in self.exported_includes: self._print_ws_path('<I>', include)
        for library in self.exported_libs:     self._print_ws_path('[L]', library)


######################################################################################


        