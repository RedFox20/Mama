from __future__ import annotations
from typing import List, TYPE_CHECKING
import os.path, sys, time

from .types.git import Git
from .types.local_source import LocalSource
from .types.asset import Asset
from .types.artifactory_pkg import ArtifactoryPkg

from .artifactory import artifactory_fetch_and_reconfigure
from .utils.system import System, console, Color, warning, build_barrier
from .utils.sub_process import SubProcess
from .utils.gdb import run_gdb, filter_gdb_arg
from .utils.gtest import run_gtest
from .utils.run import run_in_project_dir, run_in_working_dir, run_in_command_dir
from .utils.gnu_project import GnuProject
from .papa_deploy import papa_deploy_to
from . import build_names
# papa_upload is deferred to the one call site in papa_package(), see there
import mama.buildsys.msbuild as msbuild
from .utils.fileio import copy_if_needed, read_text_from
from .utils.errors import BuildError
from .utils.net import REQUIRED_DOWNLOAD_TIMEOUT, download_and_unzip, download_file
from .utils.paths import glob_with_extensions, normalized_join, path_join
from .utils.progress import get_time_str
from .utils.versions import version_at_least
from ._version import __version__
import mama.buildsys.cmake.configure as cmake
import mama.package as package

if TYPE_CHECKING:
    from .build_config import BuildConfig
    from .build_dependency import BuildDependency

# Non-library trees that the source-file TU fallback skips. For example gtest is ~90% test/ and samples/.
_NON_LIB_DIRS = ['build', 'packages', 'libs', 'out', '.git', 'test', 'tests', 'samples', 'example',
                 'examples', 'benchmark', 'benchmarks', 'doc', 'docs', 'third_party', 'thirdparty',
                 'extern', 'external', 'vendor']


######################################################################################


class BuildTarget:
    """
    Describes one configurable build target.
    This is the main mamafile-facing interface for one target.
    For project-wide configuration, @see BuildConfig in self.config.

    Customization points:
    ```
    class MyProject(mama.BuildTarget):

        workspace = 'packages'

        def dependencies(self):
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
        # Pins the last field of the artifactory archive name, in place of the commit hash. It MUST be ONE
        # raw string literal: mamafile_version.py reads it from the mamafile TEXT and never runs the file.
        self.version = ''
        self.cmake_ndk_toolchain   = '' # Custom Android toolchain file for this target only
        self.cmake_raspi_toolchain = '' # Custom Raspberry toolchain file for this target only
        self.cmake_ios_toolchain   = '' # Custom iOS toolchain file for this target only
        self.cmake_opts       = []
        self.cmake_cxxflags   = dict()
        self.cmake_cflags     = dict()
        self.cmake_ldflags    = dict()
        self.cmake_build_type = 'Debug' if config.debug else 'RelWithDebInfo'
        self.cmake_install_prefix = '.' # default CMake install: '.' = own build dir, which package()/export_libs() read
        self.cmake_lists_path = 'CMakeLists.txt' # can be relative to src_dir (default), or absolute
        self.cmake_command = config.cmake_command # allow override from config, but also from target
        self.enable_exceptions = True
        self.enable_unix_make  = False
        self.enable_ninja_build = config.prefer_ninja and config.ninja_path
        self.enable_fortran_build = False
        self.enable_cxx_build = True
        self.enable_multiprocess_build = True
        self.clean_intermediate_files = False # delete .o and .obj files after a successful build
        # Run deploy() after a build, not only under the `deploy` and `upload` commands. A project whose
        # deps ship shared libraries needs that runtime beside its binaries before a test starts.
        self.deploy_after_build = False
        self.gcc_clang_visibility_hidden = True # -fvisibility=hidden
        self.build_products = [] # executables/libs products from last build
        self.no_includes = False # no includes to export
        self.no_libs = False # no libs to export
        self.no_upload = False # nothing_to_upload(): an upload skips this target
        self.exported_includes = [] # include folders to export from this target
        self.exported_libs     = [] # libs to export from this target
        self.exported_syslibs  = [] # exported system libraries
        self.exported_assets: List[Asset] = [] # exported asset files
        self.packaging_result = '' # result of the package() step
        self._fetched = None # set by configure_phase: artifactory auto-fetch result, read by build_phase
        self._did_configure = False # guards configure() to run once across configure/build phases
        self._did_deploy = False # guards deploy() to run once, for deploy_after_build plus a deploy command
        self._build_jobs = None # scheduler-sized -j for this target's build (None -> config.jobs)
        self._out_sink = None # display sink for cmake output during a scheduled phase (None -> print)
        self.includes_root = ('','','') # if set, this is (parent_path, src_path, alias_name) for clean include deployment
        self.include_glob_filter = ['.h','.hpp','.hxx','.hh'] # default gather filter when deploying includes
        self.papa_path = None # recorded path for previous papa deployment
        self.os_windows = System.windows
        self.os_linux   = System.linux
        self.os_macos   = System.macos
        self._set_args(args)
        self.dep._update_dep_name_and_dirs(self.name)
        self.init()


    @property
    def windows(self):
        """ An MSVC build running ON Windows, so the built exe can also be run here. """
        return self.config.msvc and self.os_windows


    def _set_args(self, args: List[str]):
        if not isinstance(args, list):
            raise RuntimeError(f'BuildTarget {self.name} target args must be a list')
        for arg in args:
            if arg: self.args.append(arg)
        #console(f'Added args to {self.name}: {self.args}')


    def children(self) -> List[BuildDependency]:
        """ Get resolved child dependencies """
        return self.dep.get_children()


    def _dep_path(self, kind: str, path: str, subpath: str) -> str:
        """One directory of this dep, or a raise. A caller that reads a path the load has not
        resolved gets None, and a copy then writes outside the dependency."""
        if self.dep.skimming:
            raise RuntimeError(f'{self.name}: {kind}() is unavailable while mama explores the graph.' + \
                               ' Read the path in configure(), build() or package() instead.')
        if not path:
            raise RuntimeError(f'{self.name}: {kind}() has no path. An artifactory package has no source dir.')
        return path_join(path, subpath) if subpath else path


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
        return self._dep_path('source_dir', self.dep.src_dir, subpath)


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
        return self._dep_path('build_dir', self.dep.build_dir, subpath)


    def host_build_dir(self, subpath=''):
        """This target's build dir for the HOST platform (.../<name>/<host>), a sibling of build_dir().
        A host tool built by build_host_binary() lands here. The name follows the rules the bootstrap
        child follows, so the host arch, the compiler and the dep args all reach it."""
        host_name = build_names.host_build_dir_name(self.config, self.dep.target_args)
        host_dir = path_join(self.dep.dep_dir, host_name)
        return path_join(host_dir, subpath) if subpath else host_dir


    def _host_tools_on_disk(self, relpath) -> dict:
        """{path: mtime} for this tool in every host build dir of the dep. The caller compares two of
        these across a bootstrap, so only a file that child produced can answer.

        The predicted dir alone answers a warm probe. A dep arg changes what a tool does, so a warm
        `linux-bar` must never serve a run that asked for `linux-foo`. The scan never leaves the host
        arch, because only that platform dir opens the name."""
        dep_dir = self.dep.dep_dir
        prefix = build_names.host_view(self.config).platform.build_dir_name()
        try: names = os.listdir(dep_dir)
        except OSError: return {}
        # a dep named `linux-headers` puts its SOURCE dir beside the build dirs, and it matches the prefix
        source = os.path.basename(self.dep.src_dir) if self.dep.src_dir else ''
        found = {}
        for name in names:
            if name == source or not build_names.is_build_dir_of(name, prefix, build_names.INSTRUMENTED_TOKENS):
                continue
            candidate = path_join(dep_dir, name, relpath)
            try: found[candidate] = os.stat(candidate).st_mtime
            except OSError: pass  # the dir holds no such tool
        return found


    def build_host_binary(self, relpath, auto_build=True):
        """Makes sure a HOST-built binary of this target exists, then returns its absolute path.
        Returns None on a miss when auto_build=False. Use this for a tool needed WHILE cross-compiling,
        for example protoc, which cannot run as the arm64 android binary the target build would produce.

        Checks the host build dir first, so warm builds and repeated consumers pay nothing. On a miss,
        when auto_build=True, bootstraps via `mama <host> build target=<name>` in a host-configured
        child process. The child fetches the host artifactory package first and only builds on a miss.
        The child owns its own config, so this process builds no foreign config. When this build IS
        the host, returns the local build path.

        - relpath: binary path relative to the build dir, '.exe' is appended on Windows
        - auto_build: [True] bootstrap the host build on a miss, else return None"""
        if System.windows and not os.path.splitext(relpath)[1]:
            relpath += '.exe'  # host tools are executables, add the suffix once for every caller
        host = self.config.host_platform_name()
        if build_names.is_host_build(self.config):
            local = self.build_dir(relpath)  # already the host: the normal build produced it here
            return local if os.path.exists(local) else None
        binary = self.host_build_dir(relpath)  # the predicted dir alone: it names the args of THIS run
        if os.path.exists(binary):
            return binary
        if not auto_build:
            return None
        before = self._host_tools_on_disk(relpath)  # what the child inherits, so its own work stands out
        # sys.executable + the mama.main entry, because there is no __main__.py for `python -m mama`.
        # cwd is the root project, so the child resolves the same dependency graph.
        host_view = build_names.host_view(self.config)
        child_args = [host, 'build', f'target={self.name}', f'arch={host_view.platform.arch()}']
        # Only a command line choice travels: a mamafile preference belongs to the child's own config,
        # and forcing it here would build a host tool with a compiler the project refused. Only a linux
        # build dir names a compiler at all, which is what the host view answers.
        if host_view.linux and self.config.compiler_from_args:
            child_args.append('clang' if self.config.clang else 'gcc')
        child_cmd = 'mama ' + ' '.join(child_args)
        if self.config.print:
            console(f'  - {self.name: <16} bootstrapping host binary: {child_cmd}', color=Color.BLUE)
        argv = [sys.executable, '-c', 'from mama.main import __main__; __main__()'] + child_args
        # io_func is MANDATORY: an inherited pty lets the child draw its own live region over ours.
        # Captured, the output feeds this target's display line, log and failure replay.
        def child_output(p, line: str):
            line = line.rstrip()
            if line: console(f'  {self.name: <16} | {line}')
        status = SubProcess.run(argv, cwd=self.config.root_source_dir or os.getcwd(), io_func=child_output)
        if status != 0:
            warning(f'  - {self.name: <16} host binary bootstrap failed ({child_cmd} exited {status})')
            return None
        # The predicted dir first: a dep arg may spell a token the scan refuses, such as args=['ASAN'],
        # and the child just wrote exactly that dir. Otherwise take what the child produced, and NOTHING
        # a warm tree already held: an exit code of 0 does not prove this tool belongs to this request.
        if os.path.exists(binary): return binary
        fresh = [p for p, mtime in self._host_tools_on_disk(relpath).items() if before.get(p) != mtime]
        return max(fresh, key=os.path.getmtime) if fresh else None


    def set_artifactory_ftp(self, ftp_url, auth='store'):
        """
        Configures the remote Artifactory FTP URL where mama checks for
        prebuilt packages. If a package with the correct commit hash exists,
        mama downloads it instead of building locally.
        Only a root target may call this. A non-root call is a no-op.

        The ENV variables `MAMA_ARTIFACTORY_USER` and `MAMA_ARTIFACTORY_PASS`
        override the username and password, for use in build systems.

        - ftp_url: address of the Artifactory FTP server
        - auth: ['store'] 'store' keeps the credentials in the system keyring. Failed authentication clears them.
        ```
            def dependencies(self):
                self.set_artifactory_ftp('myserver.com', auth='store')
                self.set_artifactory_ftp('myserver.com', auth='prompt')
        ```
        NOTE: Only FTP is supported.
        """
        if not self.dep.is_root:
            return
        self.config.set_artifactory_ftp(ftp_url=ftp_url, auth=auth)


    def add_local(self, name, source_dir, mamafile=None, always_build=False, args=[],
                  version_suffix='') -> BuildDependency:
        """
        Add a local dependency. This can be a git submodule or a local folder
        that contains its own CMakeLists.txt.

        If the dependency folder has no `mamafile.py`, provide your own
        relative or absolute mamafile path.

        - name: name of the dependency
        - source_dir: path of the local folder
        - mamafile: [None] optional custom mamafile path
        - always_build: [False] always build this dependency, for chained sub-projects that do not depend on each other
        - args: [[]] extra arguments for the target mamafile, read back via `self.args`
        - version_suffix: [''] appended to the archive version, so a changed packaging recipe renames
          the package on every platform and compiler at once
        ```
        self.add_local('zlib', '3rdparty/zlib')
        self.add_local('zlib', '3rdparty/zlib', mamafile='mama/zlib.py')
        self.add_local('avdecoder', 'lib/avdecoder', always_build=True)
        ```
        """
        if self.dep.from_artifactory: # already loaded from artifactory?
            return self.get_dependency(name)
        return self.dep.add_child(LocalSource(name, source_dir, mamafile, always_build, args, version_suffix))


    def add_git(self, name, git_url, git_branch='', git_tag='', git_commit='',
                mamafile=None, shallow=True, args=[], version_suffix='') -> BuildDependency:
        """
        Add a remote GIT dependency. Mama clones it and updates it during builds.
        Use `mama update` to force update the git repositories.

        If the remote GIT repository has no `mamafile.py`, provide your own
        relative or absolute mamafile path.

        For a PUBLIC repository, use only an `https://` URL to prevent clone failures.

        - git_url: the git URL to clone from
        - git_branch: [''] branch to check out
        - git_tag: [''] tag to check out
        - git_commit: [''] commit to check out, used as the tag when git_tag is empty
        - mamafile: [None] optional custom mamafile path
        - shallow: [True] use a shallow clone
        - args: [[]] extra arguments for the child target, read back via `self.args`
        - version_suffix: [''] appended to the archive version, so a changed packaging recipe renames
          the package on every platform and compiler at once
        ```
        self.add_git('ReCpp', 'git@github.com:RedFox20/ReCpp.git')
        self.add_git('ReCpp', 'git@github.com:RedFox20/ReCpp.git', git_branch='master')
        self.add_git('opencv', 'https://github.com/opencv/opencv.git',
                     git_branch='3.4', mamafile='mama/opencv_cfg.py')
        ```
        """
        if self.dep.from_artifactory: # already loaded from artifactory?
            return self.get_dependency(name)

        if git_tag == '' and git_commit != '':
            git_tag = git_commit

        return self.dep.add_child(Git(name, git_url, git_branch, git_tag, mamafile, shallow, args, version_suffix))


    def add_artifactory_pkg(self, name, version='latest', fullname=None, version_suffix='') -> BuildDependency:
        """
        Adds an Artifactory-only dependency. Mama downloads it from the artifactory URL.

        If the remote artifactory does not have this package, the build stops with an error.

        - version: ['latest'] mama picks the matching remote package for this version
        - fullname: [None] use only this exact artifactory package as an override,
          for source-only packages and for platform-specific configuration
        - version_suffix: [''] appended to the archive version, so a changed packaging recipe renames
          the package. A `fullname` names one exact archive, so the two cannot be combined.
        ```
        self.add_artifactory_pkg('mylib', version='latest')
        self.add_artifactory_pkg('mylib', version='df76b66')
        self.add_artifactory_pkg('mylib', fullname='mylib-linux-x64-release-df76b66')
        ```
        """
        if fullname and version_suffix:
            raise RuntimeError(f'add_artifactory_pkg({name}) cannot take both fullname and version_suffix. ' + \
                               'A fullname names one exact archive, so no suffix can rename it.')
        if self.dep.from_artifactory: # already loaded from artifactory?
            return self.get_dependency(name)
        return self.dep.add_child(ArtifactoryPkg(name, version=version, fullname=fullname,
                                                 version_suffix=version_suffix))


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
        - recursive: [True] also search the children of children
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
        - dst_dep: name of the dependency that receives the defines
        - src_dep: name of the dependency that provides the products
        - include_path: name of the include define
        - libs: name of the library define
        - libfilters: [None] simple substring match over the exported libs.
          If nothing matches, mama picks the first exported lib.
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
                # if nothing matches libfilters, pick the first
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


    def add_build_dependency(self, all=None, **platforms):
        """
        Manually add a build dependency to prevent unnecessary rebuilds.

        Normally mama detects the build dependency from the packaged libraries.
        If the dependency file does not exist, the project rebuilds.
        A project with no build dependencies always rebuilds, so make sure to
        call add_build_dependency() or export_lib().

        - all: [None] path used on every platform, relative to the build directory
        - platforms: per-platform paths, @see select() for the platform names
        ```
            self.add_build_dependency('customProduct.dat')
            self.add_build_dependency(windows='custom.lib', linux='libcustom.a')
        ```
        """
        dependency = all if all else self.select(**platforms)
        if dependency:
            dependency = normalized_join(self.build_dir(), dependency)
            self.build_products.append(dependency)
            #console(f'    {self.name}.build_products += {dependency}')


    def no_export_includes(self):
        """
        Declares that this target has no includes to export. This prevents
        the automatic include export.
        ```
            def package(self):
                self.no_export_includes()
                self.export_lib('mylib.dll')
        ```
        """
        self.no_includes = True


    def no_export_libs(self):
        """
        Declares that this target has no libs to export. This prevents the
        automatic lib search. Most common for a header-only library that builds
        test binaries which mama would otherwise export.
        ```
            def package(self):
                self.no_export_libs()
                self.export_include('include')
        ```
        """
        self.no_libs = True


    def nothing_to_upload(self):
        """
        Declares that this target publishes no package, so `mama upload` skips it and reports
        the skip. An application at the root of a project builds nothing another project consumes.
        ```
            def settings(self):
                self.nothing_to_upload()
        ```
        """
        self.no_upload = True


    def export_include(self, include_path, build_dir=False,
                       includes_filter=None, as_includes_root:bool|str=False):
        """
        CUSTOM PACKAGE INCLUDES (if self.default_package() is insufficient).

        Exports an include path relative to the source directory.
        If build_dir=True, the path is relative to the build directory.
        ```
            # as_includes_root deploys as 'deploy/include/mylib/*.h' instead of 'deploy/include/installed/MyLib/*.h'
            # this keeps includes clean: #include <mylib/mylib.h> instead of <src/mylib/mylib.h>
            self.export_include('src/mylib', build_dir=False, as_includes_root='mylib')

            # a project with a separate include folder needs no extra options
            self.export_include('include')  # MyRepo/include

            # CMake installed includes in build/installed/MyLib/include
            self.export_include('installed/MyLib/include', build_dir=True)

        ```
        - include_path: the include folder to export
        - build_dir: [False] resolve include_path against the build directory
        - includes_filter: [None] replaces self.include_glob_filter, the header suffixes deployed
          during artifactory packaging. This setting applies to the entire target!
        - as_includes_root: [False] if set, this include_path is the root of all includes and mama strips
          the directory prefix: 'deploy/include/mylib/\\*' instead of 'deploy/include/src/mylib/\\*'
        """
        if includes_filter is not None:
            self.include_glob_filter = includes_filter
        return package.export_include(self, include_path, build_dir=build_dir,
                                      as_includes_root=as_includes_root)


    def export_includes(self, include_paths=[''], build_dir=False,
                        includes_filter=None):
        """
        CUSTOM PACKAGE INCLUDES (if self.default_package() is insufficient).

        Exports include paths relative to the source directory.
        If build_dir=True, the paths are relative to the build directory.
        ```
        self.export_includes(['include', 'src/moreincludes'])
        self.export_includes(['installed/include', 'installed/src/moreincludes'], build_dir=True)
        ```
        - include_paths: [['']] the include folders to export
        - build_dir: [False] resolve the paths against the build directory
        - includes_filter: [None] replaces self.include_glob_filter, the header suffixes deployed
          during artifactory packaging. This setting applies to the entire target!
        """
        if includes_filter is not None:
            self.include_glob_filter = includes_filter
        return package.export_includes(self, include_paths, build_dir=build_dir)



    def export_lib(self, relative_path, src_dir=False, build_dir=True):
        """
        CUSTOM PACKAGE LIBS (if self.default_package() is insufficient).

        Exports one lib relative to the build directory.
        If src_dir=True, the path is relative to the source directory.
        ```
        self.export_lib('mylib.a')                    # from build dir
        self.export_lib('lib/mylib.a', src_dir=True)  # from project source dir
        ```
        - relative_path: path of the lib
        - src_dir: [False] resolve against the source directory
        - build_dir: [True] resolve against the build directory
        """
        if src_dir and build_dir:
            build_dir = False
        return package.export_lib(self, relative_path, build_dir=build_dir)


    def export_libs(self, path = '.', pattern_substrings = ['.lib', '.a'], src_dir=False, build_dir=True, order=None):
        """
        CUSTOM PACKAGE LIBS (if self.default_package() is insufficient).

        Exports several libs relative to the build directory using EXTENSION MATCHING.
        If src_dir=True, the path is relative to the source directory.

        - path: ['.'] folder to search
        - pattern_substrings: [['.lib', '.a']] substrings that select the files
        - src_dir: [False] resolve against the source directory
        - build_dir: [True] resolve against the build directory
        - order: [None] partial lib names that set the link order, for the Linux linker
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
        return package.export_libs(self, path, pattern_substrings, build_dir=build_dir, order=order)


    def export_asset(self, asset, category=None, src_dir=True, build_dir=False):
        """
        Exports a single asset file from this target for later deployment.

        - asset: path of the asset file
        - category: [None] groups the assets and flattens the folder structure
        - src_dir: [True] resolve against the source directory
        - build_dir: [False] resolve against the build directory
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
        Exports asset files with a recursive glob for later deployment.

        - assets_path: folder to search
        - pattern_substrings: [[]] substrings that select the files
        - category: [None] groups the assets and flattens the folder structure
        - src_dir: [True] resolve against the source directory
        - build_dir: [False] resolve against the build directory
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
        For UNIX: finds and exports a system library so mamabuild links it automatically.

        - name: name of the system library
        - apt: [''] name of the apt package that provides the library, used in the error hint
        - required: [True] if False, a missing syslib is not an error

        :returns: TRUE if the syslib export succeeded. FALSE if required=False and the search failed.
        ```
            self.export_syslib('uuid')
            # searches the system library in this order:
            #   1. uuid
            #   2. libuuid.so
            #   3. libuuid.a

            self.export_syslib('dw', 'libdw-dev')
            # on failure, tells the user to install the package: `apt install libdw-dev`
        ```
        """
        return package.export_syslib(self, name, apt, required)


    def inject_env(self):
        """
        Injects default platform and target specific environment variables.
        Use this for a full custom build step:
        ```
            def build(self):
                self.inject_env()       # prepare platform
                self.my_custom_build()
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
        Adds C++ flags for the compilation step.
        Accepts strings, lists of strings, or space separated strings.
        ```
            self.add_cxx_flags('-Wall', '-std=c++17')
            self.add_cxx_flags('-Wall -std=c++17')
        ```
        """
        for flag in flags:
            if isinstance(flag, list): self.add_cxx_flags(*flag)
            else: self._add_dict_flag(self.cmake_cxxflags, flag)


    def add_c_flags(self, *flags):
        """
        Adds C flags for the compilation step.
        Accepts strings, lists of strings, or space separated strings.
        ```
            self.add_c_flags('-Wall', '-std=c99')
            self.add_c_flags('-Wall -std=c99')
        ```
        """
        for flag in flags:
            if isinstance(flag, list): self.add_c_flags(*flag)
            else: self._add_dict_flag(self.cmake_cflags, flag)


    def add_cl_flags(self, *flags):
        """
        Adds C AND C++ flags for the compilation step.
        Accepts strings, lists of strings, or space separated strings.
        ```
            self.add_cl_flags('-Wall', '-march=native')
            self.add_cl_flags('-Wall -march=native')
        ```
        """
        for flag in flags:
            if isinstance(flag, list): self.add_cl_flags(*flag)
            else:
                self._add_dict_flag(self.cmake_cxxflags, flag)
                self._add_dict_flag(self.cmake_cflags, flag)


    def add_ld_flags(self, *flags):
        """
        Adds flags for the linker step. Mama does no platform check here.
        Accepts strings, lists of strings, or space separated strings.
        ```
            self.add_ld_flags('-rdynamic', '-s')
            self.add_ld_flags('-rdynamic -s')
        ```
        """
        for flag in flags:
            if isinstance(flag, list): self.add_ld_flags(*flag)
            else: self._add_dict_flag(self.cmake_ldflags, flag)


    def add_platform_cxx_flags(self, **platforms):
        """
        Adds C++ flags for the active platform. @see select() for the platform names.
        ```
            self.add_platform_cxx_flags(linux='-fPIC', windows='/W4')
            self.add_platform_cxx_flags(imx8mp=['-Wall', '-std=c++17'])
        ```
        """
        flags = self.select(**platforms)
        if flags: self.add_cxx_flags(flags)


    def add_platform_ld_flags(self, **platforms):
        """
        Adds linker flags for the active platform. @see select() for the platform names.
        ```
            self.add_platform_ld_flags(windows='/LTCG', ios=['-lobjc', '-rdynamic'], linux='-rdynamic -s')
        ```
        """
        flags = self.select(**platforms)
        if flags: self.add_ld_flags(flags)


    def add_cmake_options(self, *options):
        """
        Main method for configuring CMake options.
        ```
            self.add_cmake_options('ZLIB_STATIC=TRUE', 'NO_GUI=1')
            self.add_cmake_options(['ZLIB_STATIC=TRUE', 'NO_GUI=1'])
        ```
        """
        def add(opt: str):
            # -DCMAKE_INSTALL_PREFIX is appended after cmake_opts, so a plain -D here would lose.
            key, sep, value = opt.partition('=')
            if sep and key.strip() == 'CMAKE_INSTALL_PREFIX':
                self.cmake_install_prefix = value.strip().strip('"\'')
            else:
                self.cmake_opts.append(opt)

        for option in options:
            if isinstance(option, list):
                for opt in option: add(opt)
            else:
                add(option)


    def enable_from_env(self, name, enabled='ON', force=False):
        """
        Adds a CMake option if the environment variable `name` is set to 1, ON or TRUE.
        - name: the environment variable to check
        - enabled: ['ON'] the value the CMake option gets
        - force: [False] add the option even when the variable is not set
        ```
            self.enable_from_env('BUILD_TESTS')
        ```
        """
        env = os.getenv(name)
        if force or (env and (env == '1' or env == 'ON' or env == 'TRUE')):
            self.add_cmake_options(f'{name}={enabled}')


    def add_platform_options(self, **platforms):
        """
        Selectively applies CMake options depending on the configuration platform.
        @see select() for the platform names.
        ```
            self.add_platform_options(windows='ZLIB_STATIC=TRUE', raspi='USE_NEON=ON')
        ```
        """
        defines = self.select(**platforms)
        if defines: self.cmake_opts += defines


    def select(self, **platforms):
        """
        Picks the value whose keyword names the active platform, else None. Every platform name works:
        `windows`, `linux`, `macos`, `ios`, `android`, `raspi`, `mips`, `oclea`, `xilinx`, `imx8mp`,
        plus `yocto_linux` for any Yocto board and `msvc` as an alias of `windows`.
        ```
            opts = self.select(windows='/W4', linux='-Wall')
        ```
        """
        name = self.config.platform.name
        if name in platforms: return platforms[name]
        if name == 'windows' and 'msvc' in platforms: return platforms['msvc']  # historic alias
        if 'yocto_linux' in platforms and self.yocto_linux: return platforms['yocto_linux']
        return None


    def requires_version(self, min_version: str):
        """
        Require a minimum mamabuild version for this mamafile. When the running mama is older,
        this stops the build during target load, before any configure or build work.
        The error message includes an upgrade hint, instead of a late failure on a missing API.
        - min_version: the minimum mamabuild version, for example '0.13.01'
        ```
            def settings(self):
                self.requires_version('0.13.01')
        ```
        """
        if version_at_least(__version__, min_version): return
        raise RuntimeError(f'Target {self.name} requires mamabuild >= {min_version}, but this is {__version__}.' + \
                           ' Upgrade with:  pip install --upgrade mama')


    def prefer_gcc(self):
        """ Configures the entire build chain to prefer GCC if possible """
        self.config.prefer_gcc(self.name)


    def prefer_clang(self):
        """ Configures the entire build chain to prefer Clang if possible """
        self.config.prefer_clang(self.name)


    def _get_cxx_std(self):
        return self.cmake_cxxflags.get('/std' if self.msvc else '-std', '')

    def _set_cxx_std(self, std):
        self.cmake_cxxflags['/std' if self.msvc else '-std'] = std


    def enable_cxx26(self):
        """ Enable C++26 standard """
        self._set_cxx_std('c++latest' if self.msvc else 'c++2b')

    def is_enabled_cxx26(self):
        if 'CXX26' in self.args: return True
        std = self._get_cxx_std()
        return 'c++26' in std or 'c++2c' in std or 'c++latest' in std


    def enable_cxx23(self):
        """ Enable C++23 standard """
        self._set_cxx_std('/std:c++23preview' if self.msvc else 'c++2b')

    def is_enabled_cxx23(self):
        if 'CXX23' in self.args: return True
        std = self._get_cxx_std()
        return 'c++23' in std or 'c++2b' in std or 'c++latest' in std


    def enable_cxx20(self):
        """Enable C++20 standard"""
        self._set_cxx_std(self.config.platform.cxx20_flag)

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
        Copies files and folders.
        ```
            # copies built .so into an android archive
            self.copy(self.build_dir('libAwesome.so'),
                      self.source_dir('deploy/Awesome.aar/jni/armeabi-v7a'))
        ```
        - src: source file or folder
        - dst: destination path
        - filter: [None] a string or list of strings that filter files by suffix, e.g. filter=['.h'] or filter='.hpp'
        """
        if copy_if_needed(src, dst, filter):
            if self.config.verbose: console(f'copy {src} --> {dst}')


    def copy_built_file(self, builtFile: str, copyToFolder: str):
        """
        Copies a file within the build directory.
        - builtFile: source path relative to the build directory
        - copyToFolder: destination folder relative to the build directory
        ```
            self.copy_built_file('RelWithDebInfo/libawesome.a', 'lib')
        ```
        """
        src = f'{self.build_dir()}/{builtFile}'
        dst = f'{self.build_dir()}/{copyToFolder}/{os.path.basename(builtFile)}'
        if not os.path.exists(src) and os.path.exists(dst):
            return # src is missing, but dst exists, ignore error
        if copy_if_needed(src, dst):
            if self.config.verbose: console(f'copy_built_file {src} --> {dst}')


    def copy_deployed_folder(self, src_dir: str, dst_dir: str, filter: list = None):
        """
        Copies a folder from the source directory.
        - src_dir: source folder relative to the source directory
        - dst_dir: destination folder path
        - filter: [None] a string or list of strings that filter files by suffix
        ```
            self.copy_deployed_folder('deploy/NanoMesh', 'C:/Projects/Game/Plugins')
            # --> 'C:/Projects/Game/Plugins/NanoMesh
        ```
        """
        src = self.source_dir(src_dir)
        dst = dst_dir
        if copy_if_needed(src, dst, filter):
            if self.config.verbose: console(f'copy_deployed_folder {src} --> {dst}')


    def download_file(self, remote_url: str, local_dir: str, force=False, timeout=REQUIRED_DOWNLOAD_TIMEOUT):
        """
        Downloads a file if it does not already exist. A failed download raises BuildError, which
        names the url and the reason, such as a timeout or an HTTP status.
        - remote_url: URL to download from
        - local_dir: destination folder
        - force: [False] download even when the file exists
        - timeout: [15] seconds of server silence that end the download
        ```
            self.download_file('http://example.com/file1', 'bin')
            # --> 'bin/file1'
        ```
        """
        return download_file(remote_url, local_dir, force, timeout=timeout)


    def download_and_unzip(self, remote_zip: str, extract_dir: str, unless_file_exists=None,
                           timeout=REQUIRED_DOWNLOAD_TIMEOUT):
        """
        Downloads and unzips an archive if it does not already exist.
        - remote_zip: URL of the archive
        - extract_dir: destination folder for extraction
        - unless_file_exists: [None] if this file exists, skip the download and unzip steps
        - timeout: [15] seconds of server silence that end the download
        ```
            self.download_and_unzip('http://example.com/archive.zip',
                                    'bin', 'bin/unzipped_file.txt')
            # --> 'bin/'  on success
            # --> None    on failure
        ```
        """
        return download_and_unzip(remote_zip, extract_dir, unless_file_exists, timeout)


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
        Completely disables the Ninja build for this target.
        By default, when mama detects Ninja, non-MSVC builds use Ninja for faster builds.
        Use this for example to generate an Xcode project:
        ```
            if self.ios or self.macos:
                self.disable_ninja_build()
        ```
        """
        self.enable_ninja_build = False


    def enable_fortran(self, path=''):
        """
        Enables Fortran for this target only.
        - path: [''] optional custom path or command for the Fortran compiler
        ```
            self.enable_fortran()   # attempt to autodetect fortran
            self.enable_fortran('/SysGCC/bin/gfortran')  # specify fortran explicitly
        ```
        """
        self.config.enable_fortran(path)
        self.enable_fortran_build = True


    def disable_cxx_compiler(self):
        """
        Disables all C++ options and the C++ compiler configuration.
        ```
            def configure(self):
                self.disable_cxx_compiler()
        ```
        """
        self.enable_cxx_build = False


    def nothing_to_build(self):
        """
        Call this to skip the build step every time.
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
        - name: name of the project, for example 'gmp'
        - version: version of the project, for example '6.2.1'
        - url: [''] URL to download the project, for example 'https://gmplib.org/download/gmp/{{project}}.tar.xz'
        - git: [''] git URL to clone the project from
        - build_products: [[]] the final products to build, for example
          [BuildProduct('{{installed}}/lib/libgmp.a', 'mypath/libgmp.a')].
          Supported project variables: {{installed}}, {{source}}, {{build}}
        - autogen: [False] run ./autogen.sh before ./configure
        - configure: ['configure'] the configuration command, can also be 'make config' etc
        ```
            gmp = self.gnu_project('gmp', '6.2.1', url='https://gmplib.org/download/gmp/{{project}}.tar.xz')
            gmp.configure()
        ```
        """
        return GnuProject(self, name, version, url=url, git=git, build_products=build_products,
                          autogen=autogen, configure=configure)


    def get_cc_prefix(self):
        """
        Returns the compiler prefix for cross-compiling builds, for example '/usr/bin/mipsel-linux-gnu-'.
        Returns None when the compiler has no prefix.
        """
        cc = self.config.get_preferred_compiler_paths()[0]
        filename = os.path.basename(cc)
        if filename.endswith('gcc'):
            filename = filename[:-3]
        else:
            return None # no prefix, the compiler is something like /usr/bin/gcc-11
        return path_join(os.path.dirname(cc), filename)


    def run(self, command: str, src_dir=False, exit_on_fail=True, quiet=False):
        """
        Runs a command in the build or source folder.
        Use this for custom commands or custom build systems.
        - command: the command line to run
        - src_dir: [False] if True, the command runs relative to the source directory
        - exit_on_fail: [True] exit the build when the command fails
        - quiet: [False] suppress all output. Mama still runs the command and checks the exit code.
          Use for a noisy sub-step like a script's own `git clone`.
        ```
            self.run('./configure', src_dir=True)
            self.run('make release -j7') # run in build dir
            self.run('./build_ffmpeg.sh', quiet=True) # silence a noisy custom script
        ```
        """
        run_in_project_dir(self, command, src_dir, exit_on_fail, quiet=quiet)


    def run_program(self, working_dir: str, command: str, exit_on_fail=True, env=None):
        """
        Runs any program in any directory. Use this for custom tools.
        - working_dir: the directory to run in
        - command: the command line to run
        - exit_on_fail: [True] exit the build when the program fails
        - env: [None] optional environment variables dict
        ```
            self.run_program(self.source_dir('bin'),
                             self.source_dir('bin/DbTool'))
        ```
        """
        run_in_working_dir(self, working_dir, command, exit_on_fail=exit_on_fail, env=env)


    def run_with_gdb(self, command: str, args: str, src_dir=True, gdb_by_default=True):
        """
        Runs a program with gdb if requested, otherwise runs it normally.
        To control this, add 'gdb' or 'nogdb' to args.
        Inside start(), `mama start=gdb` or `mama start=nogdb` controls the GDB enablement.
        - command: the program to run
        - args: argument string, 'gdb' and 'nogdb' are filtered out
        - src_dir: [True] run relative to the source directory
        - gdb_by_default: [True] the default when args select neither
        """
        args, gdb = filter_gdb_arg(args, gdb_by_default)
        if gdb:
            run_gdb(self, f'{command} {args}', src_dir=src_dir)
        else:
            run_in_command_dir(self, f'{command} {args}', src_dir=src_dir)


    def gdb(self, command: str, src_dir=True):
        """
        Runs a command with gdb in the source folder.
        If src_dir=False, runs in the build folder.
        ```
            self.gdb('bin/NanoMeshTests')
        ```
        """
        return run_gdb(self, command, src_dir)


    def gtest(self, executable: str, args: str, src_dir=True, gdb=False):
        """
        Runs a gtest executable.
        Writes the gtest report to {source_dir}/test/report.xml.
        - executable: which executable to run
        - args: a string of space separated options: 'gdb', 'nogdb' or a gtest fixture/test partial name
        - src_dir: [True] if True, the executable path is relative to the source directory
        - gdb: [False] if True, run with gdb
        ```
            self.gtest("bin/MyAppGtests", "nogdb", src_dir=True)
            self.gtest("bin/MyAppGtests", "MyFixtureName.TheTestName", src_dir=True)
        ```
        """
        run_gtest(self, executable, args=args, src_dir=src_dir, gdb=gdb)


    ########## Customization Points ###########


    def init(self):
        """
        Perform any initialization steps right after mama loads the mamafile.
        ```
        class MyProject(mama.BuildTarget):
            def init(self):
                self.version = '1.2.3'
        ```
        """
        pass


    def settings(self):
        """
        Define any settings at this stage. This is always the first step
        after git clone or after loading from artifactory.
        ```
        class MyProject(mama.BuildTarget):
            def settings(self):
                # only valid for root targets
                self.set_artifactory_ftp('artifacts.myftp.com', auth='store')
                self.nothing_to_build()
        ```
        """
        pass


    def dependencies(self):
        """
        Add any additional dependencies in this step,
        or configure the project for root targets.

        If this target was fetched as an artifactory package,
        mama ignores any add_git()/add_local() calls.
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
        Builds this target. The default is a CMake build.
        """
        self.cmake_build()


    def clean(self):
        """
        Perform any pre-clean steps here.
        """
        pass


    def disable_install(self):
        """
        Clears self.install_target, which disables the CMake install step.
        """
        self.install_target = ''


    def install(self):
        """
        Perform custom install steps here. The default is a CMake install.
        """
        self.cmake_install()


    def package(self):
        """
        Perform any post-build steps to package the products.
        If package() exports no includes, mama runs default_package_includes().
        If it exports no libs, mama runs default_package_libs().

        Every library should at least export some headers.
        ```
        def package(self):
            # use the built-in default packaging
            self.default_package()
            # custom export AGL as include from source folder
            self.export_includes(['AGL'])
            # custom export any .lib or .a from build folder
            self.export_libs('.', ['.lib', '.a'])

            if self.msvc:
                self.export_syslib('opengl32.lib')

            # export some asset from source folder
            self.export_asset('extras/meshes/basehead.obj')
        ```
        """
        pass


    def default_package(self):
        """Performs the default packaging steps. Mama calls this when self.package() exported nothing.
        A package() override can also call it to collect the default includes and libs.
        `no_export_includes()` and `no_export_libs()` opt each half out."""
        if not self.no_includes: self.default_package_includes()
        if not self.no_libs: self.default_package_libs()


    ## TODO: move this into `package.py`
    def default_package_includes(self):
        """Performs the default INCLUDE packaging steps. A package() override can call it to collect includes."""
        # try common C and C++ library include patterns
        if   self.export_include('include', build_dir=True):  pass
        elif self.export_include('include', build_dir=False): pass
        elif self.export_include('src',     build_dir=False, as_includes_root=self.name): pass
        elif self.export_include('',        build_dir=False): pass


    ## TODO: move this into `package.py`
    def default_package_libs(self):
        """Performs the default LIB packaging steps. A package() override can call it to collect libs."""
        if self.export_libs(self.cmake_build_type, src_dir=False): pass
        elif self.export_libs('lib', src_dir=False): pass
        elif self.export_libs('.', src_dir=False): pass


    def deploy(self):
        """
        Custom deployment stage. Built-in support for PAPA packages:
        ```
        def deploy(self):
            self.papa_deploy('deploy/NanoMesh')
        ```
        """
        self.default_deploy()


    def default_deploy(self):
        self.papa_deploy(f'deploy/{self.name}', src_dir=False)


    def papa_deploy(self, package_path, src_dir=False,
                    r_includes=False, r_dylibs=False,
                    r_syslibs=False, r_assets=False):
        """
        Creates a PAPA package, which includes:
            package_path/papa.txt
            package_path/{includes}
            package_path/{libs}
            package_path/{assets}

        - package_path: where to deploy the package
        - src_dir: [False] deploy to the source dir instead of the build dir
        - r_includes: [False] recursively export includes from dynamic libraries
        - r_dylibs: [False] recursively export all *.dll *.so *.dylib libraries
        - r_syslibs: [False] include system libraries from child dependencies
        - r_assets: [False] include assets from child dependencies

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
            return # no deploy during listing
        build_dir = not src_dir
        self.papa_path = package.target_root_path(self, package_path, build_dir=build_dir)
        papa_deploy_to(self, self.papa_path, \
            r_includes=r_includes, r_dylibs=r_dylibs, \
            r_syslibs=r_syslibs, r_assets=r_assets)


    def test(self, args):
        """
        Perform test steps here with the test args.
        `mama test arg1 arg2 arg3`
        - args: the argument string from the `mama test` command line
        ```
            def test(self, args):
                # runs an executable with GDB
                self.gdb(f'RppTests {args}')
                # or runs a gtest executable, pass 'gdb' in args to enable GDB
                self.gtest(f'bin/project_gtests', args, src_dir=True)
        ```
        """
        pass


    def start(self, args):
        """
        Start a custom process through mama.
        `mama target start=arg`
        - args: the argument string from the `mama start=...` command line
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


    def _cmake_configure_step(self, out=None):
        """CMake configure half of a build: check CMakeLists, write the proxy, inject env, run config."""
        from .dependency_chain import ensure_mama_cmake  # deferred: dependency_chain reads this module
        self.dep.ensure_cmakelists_exists()
        ensure_mama_cmake(self.dep)  # configure() can move cmake_lists_path, so the proxy follows it here
        cmake.inject_env(self)
        cmake.run_config(self, out=out) # THROWS on CMAKE failure

    def _cmake_build_step(self, out=None):
        """CMake build+install half. configure_phase sized -j from the TU probe.
        Size it here too for the serial path, where configure_phase did not run."""
        self._ensure_build_jobs()
        cmake.run_build(self, install=True, out=out) # THROWS on CMAKE failure

    def _probe_build_jobs(self) -> int:
        """TU count capped at config.jobs. Returns 0 when nothing is countable: header-only,
        artifactory, or a probe miss. Weight 0 reserves no budget and never blocks real builds,
        because such a build is a no-op."""
        try:
            n = self._count_tu()[0]
            if n > 0: return min(n, self.config.jobs)
        except Exception: pass
        return 0

    def _ensure_build_jobs(self) -> int:
        """Lazily memoizes the TU-probed -j. configure_phase sets it authoritatively after configure.
        This fills it in for the serial path and sched_debug, where configure_phase never ran."""
        if self._build_jobs is None: self._build_jobs = self._probe_build_jobs()
        return self._build_jobs

    def _reserved_cores(self) -> int:
        """Budget cores this build occupies: the memoized TU probe, equal to its actual -j, capped at
        the FULL pool. The reservation must equal -j. A heavy build with -j = all cores then reserves
        the whole pool and runs ALONE at full threads, never oversubscribed against another full-j
        build. Small builds reserve little and pack. The CPU gate still overprovisions builds that do
        not saturate. Returns 0 when unsizable: header-only, or a probe miss."""
        if not self._ensure_build_jobs(): return 0
        return min(self._build_jobs, self.config.jobs)

    def _count_tu(self) -> tuple:
        """(TU count, method) - generator-agnostic, most accurate first:
          compile_commands.json          (Ninja, or Make/VS only when export is on) -> "file" entries
          *.vcxproj                       (Visual Studio generator)                  -> <ClCompile Include=>
          CMakeFiles/**/DependInfo.cmake  (Unix Makefiles, export off)               -> one object per TU
          C/C++ source files in the source tree                                      -> cross-platform fallback
        0 when none match. The source walk skips build/vendored/test trees (see _NON_LIB_DIRS)."""
        bd = self.build_dir()
        cc = path_join(bd, 'compile_commands.json')
        if os.path.exists(cc):
            return read_text_from(cc).count('"file"'), 'compile_commands'
        if os.path.isdir(bd):
            n = sum(read_text_from(path_join(bd, fn)).count('<ClCompile Include=')
                    for fn in os.listdir(bd) if fn.endswith('.vcxproj'))
            if n > 0: return n, 'vcxproj'
            n = self._count_makefile_tus(path_join(bd, 'CMakeFiles'))
            if n > 0: return n, 'makefile'
        src = self.source_dir()
        if os.path.isdir(src):
            srcs = glob_with_extensions(src, ['.c', '.cc', '.cpp', '.cxx', '.c++', '.cu', '.m', '.mm'],
                                             exclude_dirs=_NON_LIB_DIRS)
            return len(srcs), 'source'
        return 0, 'none'

    @staticmethod
    def _count_makefile_tus(cmakefiles_dir: str) -> int:
        """Unix Makefiles generator: each target's DependInfo.cmake lists one object ('...o"') per TU."""
        if not os.path.isdir(cmakefiles_dir): return 0
        total = 0
        for dirpath, _, files in os.walk(cmakefiles_dir):
            if 'DependInfo.cmake' in files:
                total += read_text_from(path_join(dirpath, 'DependInfo.cmake')).count('.o"')
        return total

    def cmake_build(self):
        if self.config.print:
            console('\n\n#############################################################')
            console(f"CMakeBuild {self.name} ({self.cmake_build_type})")
        config_start = time.time()
        self._cmake_configure_step()
        config_stop = time.time()
        build_start = config_stop
        # Barrier: a custom build() reaches here on a worker thread. Suspend until the parallel
        # scheduler grants budget for the compile. No-op on the serial path or without a scheduler.
        with build_barrier(self._reserved_cores()):
            self._cmake_build_step()
        build_stop = time.time()
        if self.config.print:
            e_config = get_time_str(config_stop - config_start)
            e_build = get_time_str(build_stop - build_start)
            e_total = get_time_str(build_stop - config_start)
            console(f'CMakeBuild {self.name} ({self.cmake_build_type}) config {e_config}' + \
                    f' build {e_build} total {e_total}', color=Color.GREEN)


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


    def _execute_tasks(self):
        if self.dep.already_executed:
            return
        try:
            self.dep.already_executed = True
            self._execute_build_tasks()
            self._execute_deploy_tasks()
            self._execute_run_tasks()
        except Exception as err:
            console(f'  [BUILD FAILED]  {self.dep.name}  \n{err}\n\n')
            # a BuildError already reports the cause, and a trace through mama internals only buries it
            if self.config.verbose or not isinstance(err, BuildError):
                import traceback
                traceback.print_exc()
            exit(-1) # exit without further stack trace


    def try_automatic_artifactory_fetch(self):
        if not self.dep.can_fetch_artifactory(print=True, which='AUTO'):
            return None

        # Auto-fetch only for a non-root current target, a root never fetches.
        # Never for a build, deploy or upload command, those must build locally.
        is_target = not self.dep.is_root and self.is_current_target()
        is_deploy = self.config.deploy or self.config.upload
        is_build = self.config.build
        if is_target and not is_deploy and not is_build:
            fetched, _ = artifactory_fetch_and_reconfigure(self) # this will reconfigure packaging
            return fetched
        return None


    def _build_work_enabled(self) -> bool:
        """True when this target has real build work: not header-only, not from artifactory, flagged for rebuild."""
        return not self.dep.nothing_to_build and self.dep.should_rebuild and not self.dep.from_artifactory

    def _overrides(self, hook: str) -> bool:
        """True when the mamafile defines `hook` itself, instead of inheriting the empty base one."""
        return getattr(type(self), hook) is not getattr(BuildTarget, hook)

    def _has_custom_build(self) -> bool:
        """A mamafile that overrides build() fuses cmake configure+build, so the split is impossible.
        The scheduler runs it whole in build_phase and skips the separate configure step."""
        return self._overrides('build')

    def _run_configure_once(self):
        """User configure() hook, guarded to run at most once (configure_phase / build_phase / serial)."""
        if self._did_configure: return
        self._did_configure = True
        self.configure() # user customization

    def configure_phase(self, out=None):
        """Scheduled CONFIGURE job: user configure() hook + cmake configure. No-op for a
        no-work node or a custom build(), which owns its own configure inside build_phase."""
        self._out_sink = out  # capture cmake output from a custom build() too, not just the default path
        if not self._build_work_enabled() or self._has_custom_build():
            return
        self._run_configure_once()
        self._fetched = self.try_automatic_artifactory_fetch()
        if not self._fetched:
            self._cmake_configure_step(out=out)
            # Size the build weight NOW, while compile_commands.json exists, so the scheduler knows
            # the core count at BUILD launch. Left None it falls back to all cores -> serial builds.
            self._build_jobs = self._probe_build_jobs()

    def build_phase(self, out=None):
        """Scheduled BUILD job: compile if needed, then ALWAYS package, so a no-work node
        still packages its exports in dependency order. Mirrors _execute_build_tasks."""
        self._out_sink = out  # captures cmake output from a custom build()->cmake_build() too
        build_work = self._build_work_enabled()
        if build_work:
            if self._has_custom_build():
                self._run_configure_once() # configure_phase was a no-op for a custom build()
                self._fetched = self.try_automatic_artifactory_fetch()
                if not self._fetched:
                    with self._recording_deploys():
                        self.build() # user override owns configure+build, and it may deploy too
            elif not self._fetched:
                self._cmake_build_step(out=out)
            self.dep.successful_build()
            if not self._fetched:
                package.clean_intermediate_files(self)
        self._run_packaging()
        if build_work and self.deploy_after_build: self._deploy_once()

    def _execute_build_tasks(self):
        build_work = self._build_work_enabled()
        if build_work:
            self._run_configure_once() # user customization
            fetched = self.try_automatic_artifactory_fetch()
            if not fetched:
                with self._recording_deploys():
                    self.build() # user build customization, which may deploy too
            self.dep.successful_build()
            if not fetched:
                package.clean_intermediate_files(self)
        self._run_packaging()
        if build_work and self.deploy_after_build: self._deploy_once()

    def _recording_deploys(self):
        """Count what the hooks of ONE target deploy: the named target, or the root when the run names
        none. A hook that deploys another target's package counts, because this target asked for it.
        The other 30 deps deploy far more than the user asked about, so they stay out."""
        reported = self.dep.is_root if self.config.no_specific_target() else self.is_current_target()
        return self.config.deploy_stats.recording(reported)

    def _deploy_once(self):
        """Run the deploy hook at most once per run. `deploy_after_build` and the deploy pass of a
        `deploy` or `upload` command can both ask for it."""
        if self._did_deploy: return
        self._did_deploy = True
        with self._recording_deploys():
            self.deploy() # user customization

    def _run_package_hook(self):
        """Run the package() hook of the mamafile, and name the target when it fails.

        A `list` run builds nothing, so a package() that reads a build product cannot pass. A fetched
        package holds its export list in papa.txt already. Neither one fails the run, and the caller
        keeps the exports the target already had. Every other run stops."""
        try:
            self.package() # user customization
        except Exception as e:
            if not self.config.list and not self.dep.from_artifactory:
                raise BuildError(f'Package failed for target {self.name}: {e}') from e
            warning(f'  - Package {self.name: <16} INCOMPLETE, keeping the exports on disk: {e}')


    def _exports(self) -> tuple:
        return (self.exported_includes, self.exported_libs, self.exported_syslibs, self.exported_assets)


    def _set_exports(self, exports: tuple):
        (self.exported_includes, self.exported_libs, self.exported_syslibs, self.exported_assets) = exports


    def _packaging_source(self) -> str:
        """Where the exports of this target came from, for the listing."""
        if self.dep.from_artifactory and not self.dep.should_rebuild:
            return f'artifactory-cache {self.dep.artifactory_archive}'.rstrip()
        return 'target.package()'


    def _run_packaging(self):
        # package() is user mamafile code that asserts on build outputs. Wipe, upload, deploy and test walk
        # the task chain without building, so they would package artifacts never produced or just deleted.
        if not self._build_work_enabled() and not self.dep.has_usable_artifacts():
            if self.config.verbose or self.config.deploy or self.config.upload:
                warning(f'  - Target {self.name: <16} PACKAGE skipped: nothing built, no artifacts on disk')
            return

        # ALWAYS run package(). It is the only place a recipe states its export RULES: the include
        # filter, the includes_root, and the no_includes/no_libs opt-outs. papa.txt records the export
        # list and never the rules, so a fetched target that skipped the hook deployed the wrong files.
        fetched = self.dep.from_artifactory
        loaded = self._exports()  # what artifactory_load_target read out of papa.txt
        if fetched:
            self._set_exports(([], [], [], []))  # start empty, so the hook decides the whole export set

        # must populate exports via export_include()/export_libs()/export_syslib()/export_asset()
        self._run_package_hook()
        if fetched:
            # The hook owns the export RULES. papa.txt owns the LIST for every category the hook left
            # alone, so a recipe that exports includes only keeps the libs the archive recorded.
            self._set_exports(tuple(new or old for new, old in zip(self._exports(), loaded)))
        else:
            # the user provided no packaging, use the default packaging instead
            if not self.exported_includes and not self.no_includes:
                self.default_package_includes()
            if not (self.exported_libs or self.exported_syslibs) and not self.no_libs:
                self.default_package_libs()

        # A target that exports nothing has nothing to publish. Marking it here means a docs or bundle
        # target needs no declaration, and the upload validation stays a backstop rather than the rule.
        if not any(self._exports()): self.no_upload = True

        self.packaging_result = self._packaging_source()
        # A rebuild of a fetched package whose recipe now exports something else leaves a stale papa
        # file. Only a rebuild drops it: a plain build would delete the file its own shim cache needs.
        if fetched and self.dep.should_rebuild and loaded != self._exports() and self.config.print:
            console(f'  - Target {self.name} exports changed', color=Color.BLUE)
            artifactory_papa_file = self.build_dir('papa.txt')
            if os.path.exists(artifactory_papa_file):
                os.remove(artifactory_papa_file)

        if self.config.verbose:
            console(f'  - {self.name} package info loaded from [{self.packaging_result}]', color=Color.BLUE)

        # save and print exports only when the build dir exists
        if self.dep.build_dir_exists():
            self.dep.save_exports_as_dependencies(self.exported_libs)
            # print exports only for the current target
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

        # An `add_artifactory_pkg` dep is read-only and has no source, so this run built nothing to publish.
        if self.dep.dep_source.is_pkg:
            if self.config.print:
                warning(f'  - Target {self.name: <16} DEPLOY/UPLOAD skipped (artifactory pkg is read-only)')
            return

        # The shim is read-only. A re-deploy or re-upload must not overwrite its
        # papa.txt and unzipped tree. The artifactory already has the package.
        if self.dep.is_artifactory_shim():
            if self.config.print:
                warning(f'  - Target {self.name: <16} DEPLOY/UPLOAD skipped (artifactory shim, already on artifactory)')
                console(f'    To repackage from source, run: mama unshallow {self.name}')
            return

        self._deploy_once()

        if self.config.upload:
            if self.no_upload:
                if self.config.print: warning(f'  - Target {self.name: <16} UPLOAD skipped (nothing_to_upload)')
                return
            # deferred: papa_upload pulls zipfile, which costs about 28ms, and only an upload needs it
            from .papa_upload import papa_upload_to
            papa_upload_to(self, self.papa_path)


    def _require_source(self, action: str) -> bool:
        """For a command that needs source on disk (test, start, open): refuses on a shim and points
        at `mama unshallow`. Returns True when the action may proceed, False when refused."""
        if not self.dep.is_artifactory_shim():
            return True
        if self.config.print:
            warning(f'  - Target {self.name: <16} {action.upper()} skipped: artifactory shim has no source on disk')
            console(f'    To fetch source, run: mama unshallow {self.name}')
        return False


    def _run_test_task(self):
        """Run the test() hook of the mamafile, or report that there is nothing to run.
        A target without a test() runs no test, so a report of a test run would be false."""
        if not self._overrides('test'):
            if self.config.print:
                warning(f'  - Testing {self.name} SKIPPED, this mamafile defines no test()')
            return
        test_args = self.config.test.lstrip()
        if self.config.test_until_failure > 0:
            start = time.time()
            if self.config.print:
                console(f'  - Testing {self.name} {test_args} until failure (N={self.config.test_until_failure})')
            for i in range(self.config.test_until_failure):
                if self.config.print: console(f'  - Testing {self.name} {test_args} ({i+1}/{self.config.test_until_failure})')
                self.test(test_args) # should throw on failure and stop loop
            elapsed = time.time() - start
            if self.config.print:
                console(f'  - Testing {self.name} {test_args} N={self.config.test_until_failure}' +
                        f' SUCCESS in {get_time_str(elapsed)}', color=Color.GREEN)
        else:
            if self.config.print:
                console(f'  - Testing {self.name} {test_args}')
            self.test(test_args)


    def _execute_run_tasks(self):
        # a refused test must not block `start`, which asks _require_source for itself
        if self.is_test_target() and self._require_source('test'):
            self._run_test_task()

        if self.config.start:
            # start only for the current target or the root target
            if self.is_current_target() or (self.dep.is_root and self.config.no_specific_target()):
                if not self._require_source('start'):
                    return
                start_args = self.config.start.lstrip()
                if self.config.print: console(f'  - Starting {self.name} {start_args}')
                self.start(start_args)


    def _print_ws_path(self, what, path, abs_path, check_exists=True):
        def exists():
            return '' if os.path.exists(path) else '   !! (path does not exist) !!'
        display_path = path
        if not abs_path and not path.startswith('-framework'):
            if path.startswith(self.build_dir()):
                display_path = path[len(self.build_dir()) + 1:]
            elif path.startswith(self.source_dir()):
                display_path = path[len(self.source_dir()) + 1:].strip()
                if display_path == '':
                    display_path = path # for the exact source dir, keep the full path
            elif path.startswith(self.config.workspaces_root):
                display_path = path[len(self.config.workspaces_root) + 1:]
        ex = exists() if check_exists else ''
        console(f'    {what}  {display_path}{ex}')


    def print_exports(self, abs_paths=False):
        if not self.config.print:
            return
        if not (self.exported_includes or self.exported_libs or self.exported_syslibs or self.exported_assets):
            return

        console(f'  - Package {self.name}  ({self.packaging_result})')
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
        Invokes MSBuild on the specified projectfile with the given properties.
        - projectfile: the solution or project file, relative to the source directory
        - properties: [dict()] MSBuild properties
        ```
        def build(self):
            self.cmake_build()
            self.ms_build('extras/csharp/CSharpTests.sln', {
                'Configuration': 'Debug',
                'Platform': 'Any CPU',
            })
        ```
        Mama sets these default properties when the properties dict does not:
        /p:PreferredToolArchitecture=x64
        /p:Configuration=Release
        /p:Platform=x64
        """
        if self.config.print:
            console('\n#########################################')
            console(f'MSBuild {self.name} {projectfile}')
        msbuild.msbuild_build(self.config, self.source_dir(projectfile), properties)


######################################################################################


# The platform flags a mamafile reads off `self`, forwarded from config. Properties, not copies,
# so a platform switch inside init() stays visible. See README "Platform detection properties".
for _flag in ('msvc', 'linux', 'macos', 'ios', 'android', 'raspi', 'oclea',
              'xilinx', 'mips', 'imx8mp', 'yocto_linux'):
    setattr(BuildTarget, _flag, property(lambda self, name=_flag: getattr(self.config, name)))
