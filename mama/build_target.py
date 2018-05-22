import os.path, shutil
import pathlib, stat, time, subprocess, concurrent.futures
from mama.system import System, console
from mama.util import execute, save_file_if_contents_changed, glob_with_name_match, \
                    normalized_path, write_text_to
from mama.build_dependency import BuildDependency, Git
from mama.cmake_configure import run_cmake_config, run_cmake_build, cmake_default_options, \
                            cmake_inject_env, cmake_buildsys_flags, cmake_generator
import mama.util as util

######################################################################################

class BuildTarget:
    def __init__(self, name, config, dep):
        if config is None: raise RuntimeError('BuildTarget config argument must be set')
        if dep is None:    raise RuntimeError('BuildTarget dep argument must be set')
        self.config = config
        self.name = name
        self.dep = dep
        self.install_target   = 'install'
        self.cmake_ndk_toolchain = f'{config.android_ndk_path}/build/cmake/android.toolchain.cmake' if config.android_ndk_path else ''
        self.cmake_ios_toolchain = ''
        self.cmake_opts       = []
        self.cmake_cxxflags   = ''
        self.cmake_ldflags    = ''
        self.cmake_build_type = 'Debug' if config.debug else 'RelWithDebInfo'
        self.enable_exceptions = True
        self.enable_unix_make  = False
        self.enable_ninja_build = True and config.ninja_path # attempt to use Ninja
        self.enable_multiprocess_build = True
        self.build_dependencies = [] # dependency files
        self.exported_includes = [] # include folders to export from this target
        self.exported_libs     = [] # libs to export from this target


    def get_full_path(self, path):
        if path and not os.path.isabs(path):
            if self.dep.mamafile: # if setting mamafile, then use mamafile folder:
                path = os.path.join(os.path.dirname(self.dep.mamafile), path)
            else:
                path = os.path.join(self.dep.src_dir, path)
            path = normalized_path(path)
        return path


    def get_mamafile_path(self, name, mamafile):
        if mamafile:
            return self.get_full_path(mamafile)
        maybe_mamafile = self.get_full_path(f'mama/{name}.py')
        if os.path.exists(maybe_mamafile):
            return maybe_mamafile
        return mamafile


    ###
    # Add a local dependency
    def add_local(self, name, source_dir, mamafile=None):
        src      = self.get_full_path(source_dir)
        mamafile = self.get_mamafile_path(name, mamafile)
        dependency = BuildDependency.get(name, self.config, BuildTarget, \
                        workspace=self.dep.workspace, src=src, mamafile=mamafile)
        self.dep.children.append(dependency)


    ###
    # Add a remote dependency
    def add_git(self, name, git_url, git_branch='', git_tag='', mamafile=None):
        git = Git(git_url, git_branch, git_tag)
        mamafile = self.get_mamafile_path(name, mamafile)
        dependency = BuildDependency.get(name, self.config, BuildTarget, \
                        workspace=self.dep.workspace, git=git, mamafile=mamafile)
        self.dep.children.append(dependency)


    def get_dependency(self, name):
        for dep in self.dep.children:
            if dep.name == name:
                return dep
        raise KeyError(f"BuildTarget {self.name} has no child dependency named '{name}'")


    def add_dependency(self, name, depends_on_name):
        d = self.get_dependency(name)
        dependency = self.get_dependency(depends_on_name)
        d.depends_on.append(dependency)


    ## Injects products from `src_dep` into `dst_dep` as CMake defines
    ## Name of defines is given via `include_path` and `libs` params
    ## `libfilters` does simple string matching; if nothing matches, the first export lib is chosen
    ## Ex:
    ##     self.inject_products('libpng', 'zlib', 'ZLIB_INCLUDE_DIR', 'ZLIB_LIBRARY', 'zlibstatic')
    def inject_products(self, dst_dep, src_dep, include_path, libs, libfilters=None):
        dst_dep = self.get_dependency(dst_dep)
        src_dep = self.get_dependency(src_dep)
        dst_dep.depends_on.append(src_dep)
        dst_dep.product_sources.append( (src_dep, include_path, libs, libfilters) )


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


    # Gets target products as a tuple: (include_paths=[], libs=[])
    def get_target_products(self, target_name):
        dep = self.get_dependency(target_name)
        target:BuildTarget = dep.target
        return (target.exported_includes, target.exported_libs)


    ## Adds a build dependency to prevent unnecessary rebuilds
    def add_build_dependency(self, all='', windows='', android='', ios='', linux='', mac=''):
        dependency = all if all else self.select(windows, android, ios, linux, mac)
        if dependency:
            dependency = normalized_path(os.path.join(self.dep.build_dir, dependency))
            self.build_dependencies.append(dependency)
            #console(f'    {self.name}.build_dependencies += {dependency}')


    ####
    # Export includ path relative to source directory
    #  OR if build_dir=True, then relative to build directory
    def export_include(self, include_path, build_dir=False):
        root = self.dep.build_dir if build_dir else self.dep.src_dir
        include_path = normalized_path(os.path.join(root, include_path))
        #console(f'export_include={include_path}')
        if os.path.exists(include_path):
            if not include_path in self.exported_includes:
                self.exported_includes.append(include_path)
            return True
        return False

    ####
    # Export include paths relative to source directory
    #  OR if build_dir=True, then relative to build directory
    def export_includes(self, include_paths=[''], build_dir=False):
        self.exported_includes = []
        for include_path in include_paths:
            self.export_include(include_path, build_dir)


    ####
    # Export lib relative to build directory
    #  OR if src_dir=True, then relative to source directory
    def export_lib(self, relative_path, src_dir=False):
        root = self.dep.src_dir if src_dir else self.dep.build_dir
        path = normalized_path(os.path.join(root, relative_path))
        if os.path.exists(path):
            self.exported_libs.append(path)
            self.remove_duplicate_export_libs()


    ####
    # Export libs relative to build directory
    #  OR if src_dir=True, then relative to source directory
    def export_libs(self, path = '.', pattern_substrings = ['.lib', '.a'], src_dir=False):
        root = self.dep.src_dir if src_dir else self.dep.build_dir
        path = os.path.join(root, path)
        self.exported_libs = glob_with_name_match(path, pattern_substrings)
        self.remove_duplicate_export_libs()
        return len(self.exported_libs) > 0


    def remove_duplicate_export_libs(self):
        unique = dict()
        for lib in self.exported_libs:
            unique[os.path.basename(lib)] = lib
        self.exported_libs = list(unique.values())


    def inject_env(self):
        cmake_inject_env(self)


    # Adds C / C++ flags for compilation step
    def add_cxx_flags(self, msvc='', clang=''):
        self.cmake_cxxflags += ' '
        self.cmake_cxxflags += msvc if self.config.windows else clang


    # Adds linker flags depending on configuration platform
    def add_linker_flags(self, windows='', android='', ios='', linux='', mac=''):
        flags = self.select(windows, android, ios, linux, mac)
        if flags: self.cmake_ldflags += ' '+flags


    ## 
    # Main method for configuring CMake options
    # Ex:
    #   self.add_cmake_options('ZLIB_STATIC=TRUE')
    #
    def add_cmake_options(self, *options):
        for option in options:
            if isinstance(option, list): self.cmake_opts += option
            else:                        self.cmake_opts.append(option)

    
    ## Selectively applies CMake options depending on configuration platform
    def add_platform_options(self, windows=[], android=[], ios=[], linux=[], mac=[]):
        defines = self.select(windows, android, ios, linux, mac)
        if defines: self.cmake_opts += defines


    def select(self, windows, linux, macos, ios, android):
        if   self.config.windows and windows: return windows
        elif self.config.linux   and linux:   return linux
        elif self.config.macos   and macos:   return macos
        elif self.config.ios     and ios:     return ios
        elif self.config.android and android: return android
        return None


    def enable_cxx17(self):
        self.cmake_cxxflags += ' /std:c++17' if self.config.windows else ' -std=c++17'
    def enable_cxx14(self):
        self.cmake_cxxflags += ' /std:c++14' if self.config.windows else ' -std=c++14'
    def enable_cxx11(self):
        self.cmake_cxxflags += ' /std:c++11' if self.config.windows else ' -std=c++11'


    def copy_built_file(self, builtFile, copyToFolder):
        src = f'{self.dep.build_dir}/{builtFile}'
        dst = f'{self.dep.build_dir}/{copyToFolder}'
        shutil.copy(src, dst)


    # def download_file(self, remote_url, local_dir, force=False):
    #     util.download_file(remote_url, local_dir, force)


    # def download_and_unzip(self, remote_zip, extract_dir):
    #     util.download_and_unzip(remote_zip, extract_dir)


    def nothing_to_build(self):
        self.dep.nothing_to_build = True
        self.dep.should_rebuild = False


    # Run a command in the build folder
    def run(self, command):
        execute(f'cd {self.dep.build_dir} && {command}', echo=True)

    
    # Run a command with gdb in the build folder
    def gdb(self, command):
        gdb = f'gdb -batch -ex "run" -ex "bt" {command}'
        execute(f'cd {self.dep.build_dir} && {gdb}', echo=True)

    ########## Customization Points ###########


    ###
    # Add any dependencies in this step
    #   self.add_local(...)
    #   self.add_remote(...)
    def dependencies(self):
        pass


    ###
    # Perform any pre-build steps here
    def configure(self):
        pass


    ### 
    # Perform any pre-clean steps here
    def clean(self):
        pass


    ###
    # Perform any post-build steps to package the products
    def package(self):
        # try multiple common/popular C and C++ library include patterns
        if   self.export_include('include', build_dir=True):  pass
        elif self.export_include('include', build_dir=False): pass
        elif self.export_include('src',     build_dir=False): pass
        elif self.export_include('',        build_dir=False): pass

        # default export from {build_dir}/{cmake_build_type}
        if self.export_libs(self.cmake_build_type, src_dir=False): pass
        elif self.export_libs('lib', src_dir=False): pass


    ###
    # Perform test steps here
    def test(self):
        pass



    ############################################


    def install(self):
        console('\n\n#############################################################')
        console(f"CMake install {self.name} ...")
        run_cmake_build(self)


    def clean_target(self):
        self.dep.clean()


    def ensure_cmakelists_exists(self):
        cmakelists = os.path.join(self.dep.src_dir, 'CMakeLists.txt')
        if not os.path.exists(cmakelists):
            raise IOError(f'Could not find {cmakelists}! Add a CMakelists.txt, or \
                            add `self.nothing_to_build()` to configuration step. \
                            Also note that filename CMakeLists.txt is case sensitive.')
    

    def run_build_task(self):
        console('\n\n#############################################################')
        console(f"CMake build {self.name}")
        self.ensure_cmakelists_exists()
        def cmake_flags():
            flags = ''
            options = self.cmake_opts + cmake_default_options(self) + self.get_product_defines()
            for opt in options: flags += '-D'+opt+' '
            return flags

        self.inject_env()
        run_cmake_config(self, cmake_flags())
        run_cmake_build(self, cmake_buildsys_flags(self))
        self.dep.save_git_status()


    ## Build only this target
    def execute_tasks(self):
        if self.dep.already_executed:
            return
        
        self.dep.already_executed = True

        if self.dep.should_rebuild and not self.dep.nothing_to_build:

            self.configure() # user customization
            if not self.dep.nothing_to_build:
                self.run_build_task()
        
        self.package() # user customization
        self.dep.save_exports_as_dependencies(self.exported_libs)

        self.print_exports()

        if self.config.test:
            self.test()


    def print_ws_path(self, what, path):
        n = len(self.config.workspaces_root) + 1
        exists = '' if os.path.exists(path) else '   !! (path does not exist) !!' 
        console(f'    {what} {path[n:]}{exists}')


    def print_exports(self):
        console(f'  - Package {self.name}')
        n = len(self.config.workspaces_root) + 1
        for include in self.exported_includes: self.print_ws_path('inc', include)
        for library in self.exported_libs:     self.print_ws_path('lib', library)


######################################################################################


        