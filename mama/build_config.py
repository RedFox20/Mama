from __future__ import annotations
import os, sys, tempfile, platform, psutil, shutil
from typing import List, TYPE_CHECKING
from mama.platforms.oclea import Oclea
from mama.platforms.xilinx import Xilinx
from mama.platforms.mips import Mips
from mama.platforms.android import Android
from mama.platforms.imx8mp import Imx8mp
from mama.platforms.generic_yocto import GenericYocto
import mama.util as util
from .utils.system import System, console, Color
from .utils.sub_process import execute, execute_piped

if System.linux:
    import distro

if TYPE_CHECKING:
    from .build_dependency import BuildDependency

###
# Mama Build Configuration is created only once in the root project working directory
# This configuration is then passed down to dependencies
#
class BuildConfig:
    def __init__(self, args):
        # commands
        self.list    = False
        self.build   = False
        self.clean   = False
        self.rebuild = False
        self.update  = False
        self.deploy  = False
        # if root mamafile has defined an artifacts URL
        # this will upload deploy archive through SFTP
        self.upload  = False
        # currently for uploads only, uploads only if package already not uploaded
        self.if_needed = False
        # if `art` is specified, then artifactory download is mandatory, no source builds are done
        self.force_artifactory = False
        # if `noart` is specified, then artifactory is temporarily ignored
        self.disable_artifactory = False
        self.reclone   = False
        self.dirty     = False # marks a target for rebuild on next build even if it's up to date
        self.deps_only = False # only execute build/rebuild/clean on dependencies, not the main target
        self.unshallow = False  # by default, git clones are shallow, this allows unshallowing
        self.run_cmake_configure = False # if True, forces running CMake configure step even if target doesn't need rebuild
        self.mama_init = False
        self.print     = True
        self.verbose   = False
        self.test      = ''
        self.start     = ''
        self.with_tests = False # forces -DENABLE_TESTS=ON
        self.test_until_failure = 0 # if > 0, runs test executable in a loop until it fails, useful for catching flaky tests
        self.sanitize  = None # gcc/clang: -fsanitize=[thread|leak|address|undefined]
        self.coverage  = None # gcc/clang: gcov | msvc: /fsanitize-coverage=edge
        self.coverage_report = None # runs gcovr to generate coverage report
        self.enable_clang_tidy = False # enables clang-tidy static analysis during build
        self.clang_tidy_path = None # resolved path to clang-tidy executable
        # supported platforms
        self.msvc    = False # whether this is a MSVC build on Windows
        self.linux   = False
        self.macos   = False
        self.ios     = False
        self.android : Android = None
        self.raspi   = False # TODO: modernize to Class based impl
        self.mips : Mips = None
        self.oclea : Oclea = None
        self.xilinx : Xilinx = None
        self.imx8mp : Imx8mp = None
        self.yocto_linux : GenericYocto = None # whether this is a generic Yocto Linux embedded platform (e.g. Oclea, Xilinx, IMX8MP)
        # cmake customization
        self.cmake_command = 'cmake' # by default, use whatever cmake is in PATH
        # compiler preferences
        self.clang = False
        self.gcc   = False
        self.clang_path = ''
        self.gcc_path = ''
        # can be used to overide C and C++ compiler paths
        self.cc_path = ''
        self.cxx_path = ''
        self.cxx_version = '' # c++ compiler version, eg '8.3.0' for gcc 8.3.0
        # If compiler specificed from command line
        # using `mama build gcc` or `mama build clang`
        self.compiler_cmd = False
        self.fortran = ''
        # build optimization
        self.release = True
        self.debug   = False
        # valid architectures: x86, x64, arm, arm64
        self.arch    = None
        self.distro  = None  # distro information (name, major, minor)
        self.jobs    = psutil.cpu_count()
        self.target  = None
        self.flags   = None
        self.open    = None
        # use this to customize ios sdk version
        self.ios_version   = '16.0' # 16: ios 16
        # use this to customize macos sdk version
        self.macos_version = '13.0' # 13: macos 13
        ## Artifactory URL for dependency uploads and downloads
        self.artifactory_ftp = None
        self.artifactory_auth = None
        ## Ninja
        self.ninja_path = self.find_ninja_build()
        self.prefer_ninja = not System.windows # do not prefer ninja on Windows by default
        ## MSVC, MSBuild
        self._visualstudio_path = None
        self._visualstudio_cmake_id = None
        self._msbuild_path = None
        self._msvctools_path = None
        ## Raspberry PI - Raspi
        self.raspi_compilers  = ''  ## Raspberry g++ and gcc
        self.raspi_system     = ''  ## path to Raspberry system libraries
        self.raspi_include_paths = [] ## path to additional Raspberry include dirs
        ## Convenient installation utils:
        self.convenient_install = []
        ## Workspace and parsing
        self.parallel_load = False  ## Whether to load dependencies in parallel?
        self.global_workspace = False
        if System.windows:
            self.workspaces_root = util.normalized_path(os.getenv('HOMEPATH'))
        else:
            self.workspaces_root = os.getenv('HOME')
        self.unused_args = []
        self.loaded_dependencies : dict[str, BuildDependency] = {}
        self.parse_args(args)
        self.check_platform()


    def parse_args(self, args: List[str]):
        for arg in args:
            if   arg == 'list':      self.list    = True
            elif arg == 'build':     self.build   = True
            elif arg == 'clean':     self.clean   = True
            elif arg == 'rebuild':   self.rebuild = True
            elif arg == 'update':    self.update  = True
            elif arg == 'deploy':    self.deploy  = True
            elif arg == 'upload':    self.upload  = True
            elif arg == 'if_needed': self.if_needed = True
            elif arg == 'art':       self.force_artifactory = True
            elif arg == 'noart':     self.disable_artifactory = True
            # Updates, Builds and Deploys the project as a package
            elif arg == 'serve':
                self.rebuild = True
                self.update = True
                self.deploy = True
                self.upload = True
            elif arg == 'reclone':
                console('WARNING: Argument `reclone` is deprecated, use `wipe` instead.')
                self.reclone = True
            elif arg == 'wipe':      self.reclone = True
            elif arg == 'dirty':     self.dirty = True
            elif arg == 'deps_only': self.deps_only = True
            elif arg == 'unshallow': self.unshallow = True
            elif arg == 'configure':
                self.run_cmake_configure = True
                self.build = True # configure implies a build
            elif arg == 'init':      self.mama_init = True
            elif arg == 'silent':    self.print = False
            elif arg == 'verbose':   self.verbose = True
            elif arg == 'parallel':  self.parallel_load = True
            elif arg == 'all':       self.target = 'all'
            elif arg == 'test':      self.test = ' ' # no test arguments
            elif arg == 'start':     self.start = ' ' # no start arguments
            elif arg == 'with_tests': self.with_tests = True
            elif arg.startswith('sanitize='): self.add_sanitizer_option(arg[9:])
            elif arg == 'asan':    self.add_sanitizer_option('address')
            elif arg == 'lsan':    self.add_sanitizer_option('leak')
            elif arg == 'tsan':    self.add_sanitizer_option('thread')
            elif arg == 'ubsan':   self.add_sanitizer_option('undefined')
            elif arg == 'clang-tidy': self.enable_clang_tidy = True
            elif arg.startswith('coverage='): self.add_coverage_option(arg[9:])
            elif arg == 'coverage': self.add_coverage_option()
            elif arg == 'coverage-report':
                self.coverage_report = '.'
                self.add_coverage_option() # also enable coverage if reporting is requested
            elif arg.startswith('coverage-report='):
                self.coverage_report = arg[16:]
                self.add_coverage_option()
            elif arg == 'windows': self.set_platform(msvc=True)
            elif arg == 'msvc':    self.set_platform(msvc=True)
            elif arg == 'linux':   self.set_platform(linux=True)
            elif arg == 'macos':   self.set_platform(macos=True)
            elif arg == 'ios':     self.set_platform(ios=True)
            elif arg == 'android': self.set_platform(android=True)
            elif arg == 'raspi':   self.set_platform(raspi=True)
            elif arg == 'oclea':   self.set_platform(oclea=True)
            elif arg == 'xilinx':  self.set_platform(xilinx=True)
            elif arg == 'mips':    self.set_platform(mips=True)
            elif arg == 'imx8mp':  self.set_platform(imx8mp=True)
            elif arg == 'x86':     self.set_arch('x86')
            elif arg == 'x64':     self.set_arch('x64')
            elif arg == 'arm':     self.set_arch('arm')
            elif arg == 'arm64':   self.set_arch('arm64')
            elif arg == 'aarch64':
                console('warning: aarch64 is the same as arm64, setting to arm64')
                self.set_arch('arm64')
            elif arg == 'clang':
                self.gcc = False
                self.clang = True
                self.compiler_cmd = True
            elif arg == 'gcc':
                self.gcc = True
                self.clang = False
                self.compiler_cmd = True
            elif arg == 'fortran': self.fortran = self.find_default_fortran_compiler()
            elif arg.startswith('fortran='): self.fortran = arg[8:]
            elif arg == 'release': self.set_build_config(release=True)
            elif arg == 'debug':   self.set_build_config(debug=True)
            elif arg == 'open':    self.open = 'root'
            # Open a specific target source dir for editing with VSCode or Visual Studio
            # Ex old: mama open=ReCpp
            # Ex new: mama open ReCpp
            elif arg.startswith('open='):   self.open = arg[5:]
            elif arg.startswith('jobs='):   self.jobs = int(arg[5:])
            # Sets the target to build/update/clean
            # This is superceded by automatic target lookup
            # Ex old: mama build target=opencv
            # Ex new: mama build opencv
            elif arg.startswith('target='): self.target = arg[7:]
            # Adding arguments for tests runner
            # Ex: mama build test="nogdb threadpool"
            # Ex: mama build test=nogdb test=threadpool
            # Ex: mama build test=nogdb,threadpool
            elif arg.startswith('test='):   self.test = self.join_args(self.test, arg[5:])
            # Adding arguments for test runner to run tests in a loop until failure, useful for catching flaky tests
            # Ex: mama build test="my_flaky_test" test_until_failure=100
            elif arg == 'test_until_failure': self.test_until_failure = 100 # arbitrary default
            elif arg.startswith('test_until_failure='): self.test_until_failure = int(arg[19:]) # set number of iterations to run tests until failure
            # Calls target.start with the specified arguments
            # Ex: mama build start=verify
            elif arg.startswith('start='):  self.start = self.join_args(self.start, arg[6:])
            elif arg.startswith('arch='):   self.set_arch(arg[5:])
            # Add additional compiler flags
            elif arg.startswith('flags='):  self.flags = self.join_args(self.flags, arg[6:])
            # Ex: mama build android-24
            elif arg.startswith('android-'):
                self.set_platform(android=True)
                self.android.android_api = arg
            elif arg.startswith('ndk-'):
                self.set_platform(android=True)
                self.android.ndk_version = arg[4:] # can be `ndk-28` or `ndk-28.2` etc
            elif arg.startswith('install-clang-'): self.convenient_install.append('clang-' + arg[14:])
            elif arg.startswith('install-gcc-'):   self.convenient_install.append('gcc-' + arg[12:])
            elif arg == 'install-msbuild': self.convenient_install.append('msbuild')
            elif arg.startswith('install-ndk-'): self.convenient_install.append('ndk-' + arg[12:])
            else:
                self.unused_args.append(arg)
            continue


    # modifies existing `args` by parsing and appending `arg` contents
    def join_args(self, args, arg):
        if arg[0] == '"' and arg[-1] == '"':
            arg = arg[1:-1]
        elif ',' in arg:
            arg = ' '.join(arg.split(','))
        if not args:
            return arg
        return args + ' ' + arg


    def set_platform(self, msvc=False, linux=False, macos=False, \
                           ios=False, android=False, raspi=False, \
                           oclea=False, mips=False, xilinx=False, imx8mp=False):
        """ Ensures only a single platform is set """

        platforms = [False]*10
        if   msvc:   platforms[0] = True
        elif linux:  platforms[1] = True
        elif macos:  platforms[2] = True
        elif ios:    platforms[3] = True
        elif android:platforms[4] = True
        elif raspi:  platforms[5] = True
        elif oclea:  platforms[6] = True
        elif mips:   platforms[7] = True
        elif xilinx: platforms[8] = True
        elif imx8mp: platforms[9] = True

        def get_new_value(old_value, enable, type=None):
            if old_value and not enable:
                return None if type else False
            if enable and not old_value:
                return type(self) if type else True
            return old_value
        self.msvc    = get_new_value(self.msvc,    platforms[0])
        self.linux   = get_new_value(self.linux,   platforms[1])
        self.macos   = get_new_value(self.macos,   platforms[2])
        self.ios     = get_new_value(self.ios,     platforms[3])
        self.android = get_new_value(self.android, platforms[4], Android)
        self.raspi   = get_new_value(self.raspi,   platforms[5])
        self.oclea   = get_new_value(self.oclea,   platforms[6], Oclea)
        self.mips    = get_new_value(self.mips,    platforms[7], Mips)
        self.xilinx  = get_new_value(self.xilinx,  platforms[8], Xilinx)
        self.imx8mp  = get_new_value(self.imx8mp,  platforms[9], Imx8mp)

        # convenience alias for detecting embedded Yocto Linux platforms (e.g. Oclea, Xilinx, IMX8MP)
        if self.imx8mp: self.yocto_linux = self.imx8mp
        if self.oclea:  self.yocto_linux = self.oclea
        if self.xilinx: self.yocto_linux = self.xilinx
        return True


    def is_platform_set(self):
        return self.msvc or self.linux or self.macos \
            or self.ios or self.android or self.raspi or self.mips \
            or self.yocto_linux


    def check_platform(self):
        if not self.is_platform_set():
            # choose MSVC if user did not specify `mama build gcc` on windows system
            msvc = System.windows and not (self.gcc or self.clang)
            self.set_platform(msvc=msvc, linux=System.linux, macos=System.macos)
            if not self.is_platform_set():
                raise RuntimeError(f'Unsupported platform {sys.platform}: Please specify platform!')
        if not self.msvc and not (self.gcc or self.clang):
            self.gcc = True # default to GCC on non-MSVC platforms

        # set defaults if arch was not specified
        if not self.arch:
            # macos has now moved to arm64 starting with M1 series chips
            if self.macos:        self.set_arch('arm64')
            elif self.ios:        self.set_arch('arm64')
            elif self.android:    self.set_arch('arm64')
            elif self.yocto_linux:self.set_arch('arm64')
            elif self.raspi:      self.set_arch('arm')
            elif self.mips:       self.set_arch(self.mips.mips_arch)
            else:
                if System.aarch64:  self.set_arch('arm64')
                elif System.x86_64: self.set_arch('x64')
                else:               self.set_arch('x86')

        # Arch itself is validated in set_arch(),
        # however we need to validate if arch is allowed on platform
        if self.arch:
            if self.yocto_linux and self.arch != 'arm64':
                raise RuntimeError(f'Unsupported arch={self.arch} on {self.yocto_linux.name} platform! Supported=arm64')
            if self.linux and self.arch == 'arm':
                raise RuntimeError(f'Unsupported arch={self.arch} on linux platform! Build with android instead')
            if self.raspi and self.arch != 'arm':
                raise RuntimeError(f'Unsupported arch={self.arch} on raspi platform! Supported=arm')
            if self.mips and self.arch not in self.mips.supported_arches:
                raise RuntimeError(f'Unsupported arch={self.arch} on MIPS platform! Supported={self.mips.supported_arches}')

        if self.enable_clang_tidy:
            # resolve clang-tidy path based on platform
            self.set_clang_tidy_path(self.clang_tidy_path)


    def get_distro_info(self):
        if self.distro:
            return self.distro
        if self.msvc:
            version = platform.version().split('.') + ['0']
            self.distro = (self.name(), int(version[0]), int(version[1]))
        elif self.macos:
            version = self.macos_version.split('.') + ['0']
            self.distro = (self.name(), int(version[0]), int(version[1]))
        elif self.ios:
            version = self.ios_version.split('.') + ['0']
            self.distro = (self.name(), int(version[0]), int(version[1]))
        elif self.android:
            version = self.android.android_api.split('-')[1]
            self.distro = (self.name(), int(version), 0)
        elif self.yocto_linux:
            self.distro = self.yocto_linux.distro_version
        elif self.raspi:
            # TODO: RASPI version
            self.distro = (self.name(), 0, 0)
        elif self.mips:
            # TODO: MIPS version
            self.distro = (self.name(), self.mips.toolchain_major, self.mips.toolchain_minor)
        elif self.linux:
            try:
                dist = distro.info()
                major = int(dist['version_parts']['major'])
                minor = int(dist['version_parts']['minor'])
                self.distro = (dist['id'], major, minor)
            except Exception as err:
                console(f'Failed to parse linux distro; falling back to Ubuntu 16.04 LTS: {err}', color=Color.RED)
                self.distro = ('ubuntu', 16, 4)
        else:
            self.distro = (platform.system().lower(), int(platform.release()), 0)
        return self.distro


    def set_arch(self, arch):
        arches = ['x86', 'x64', 'arm', 'arm64', 'mips', 'mipsel', 'mips64', 'mips64el']
        if not arch in arches:
            raise RuntimeError(f"Unrecognized architecture {arch}! Valid options are: {arches}")
        self.arch = arch


    def is_64bit_build(self):
        return (self.arch == 'x64' or self.arch == 'arm64')


    def name(self):
        if self.msvc:    return 'windows' # TODO: maybe rename to MSVC?
        if self.linux:   return 'linux'
        if self.macos:   return 'macos'
        if self.ios:     return 'ios'
        if self.android: return 'android'
        if self.raspi:   return 'raspi'
        if self.yocto_linux: return self.yocto_linux.name # imx8mp, oclea, xilinx
        if self.mips:        return self.mips.name
        return 'build'


    ## These are the hard references to all build directory variations
    ## All parts of the codebase should use these, instead of raw strings
    ## This will avoid accidental mismatches
    def build_dir_win64(self): return 'windows'
    def build_dir_win32(self): return 'windows32'
    def build_dir_winarm64(self): return 'winarm'
    def build_dir_winarm32(self): return 'winarm32'
    def build_dir_linux64(self): return 'linux'
    def build_dir_linux32(self): return 'linux32'
    def build_dir_linuxarm64(self): return 'linuxarm' # arm64
    def build_dir_macosarm64(self): return 'macosarm' # arm64
    def build_dir_macos64(self): return 'macos' # x64
    def build_dir_ios(self): return 'ios' # arm64
    def build_dir_android64(self): return 'android'
    def build_dir_android32(self): return 'android32'
    def build_dir_raspi32(self): return 'raspi'
    def build_dir_oclea64(self): return Oclea.BUILD_DIR
    def build_dir_xilinx64(self): return Xilinx.BUILD_DIR
    def build_dir_imx8mp(self): return Imx8mp.BUILD_DIR
    def build_dir_mips(self): return 'mips'
    def build_dir_default(self): return 'build'


    def platform_build_dir_name(self):
        """
        Gets the build folder name depending on platform and architecture.
        By default 64-bit architectures use the platform name, eg 'windows' or 'linux'
        And 32-bit architectures add a suffix, eg 'windows32' or 'linux32'
        """
        # WARNING: This needs to be in sync with dependency_chain.py: _save_mama_cmake !!!
        if self.msvc:
            if self.is_target_arch_x64(): return self.build_dir_win64()
            if self.is_target_arch_x86(): return self.build_dir_win32()
            if self.is_target_arch_armv7(): return self.build_dir_winarm32()
            return self.build_dir_winarm64()
        if self.yocto_linux:
            # Only arm64 i.MX8M Plus, Xilinx Zync, Oclea Aarch64...
            return self.yocto_linux.build_dir
        if self.linux:
            if self.is_target_arch_x64(): return self.build_dir_linux64()
            if self.is_target_arch_arm64(): return self.build_dir_linuxarm64()
            return self.build_dir_linux32()
        if self.macos: # Apple dropped 32-bit support
            # and new default should be arm64 starting from M1 series chips
            if self.is_target_arch_arm64(): return self.build_dir_macosarm64()
            return self.build_dir_macos64()
        if self.ios: # Apple dropped 32-bit support
            return self.build_dir_ios()
        if self.android:
            if self.is_target_arch_arm64(): return self.build_dir_android64()
            return self.build_dir_android32()
        if self.raspi: return self.build_dir_raspi32()  # Only 32-bit raspi
        if self.mips: return self.build_dir_mips()

        return self.build_dir_default()


    def set_build_config(self, release=False, debug=False):
        self.release = release
        self.debug   = debug
        return True


    def set_artifactory_ftp(self, ftp_url: str, auth='store'):
        """ @see BuildTarget.set_artifactory_ftp() for documentation """
        self.artifactory_ftp = ftp_url
        self.artifactory_auth = auth


    def prefer_clang(self, target_name):
        if not self.linux or self.raspi or self.clang: return
        if not self.compiler_cmd:
            self.clang = True
            self.gcc   = False
            self.compiler_cmd = True
            if self.print:
                console(f'Target {target_name} requests Clang. Using Clang since no explicit compiler flag passed.')
        else:
            if self.print:
                console(f'Target {target_name} requested Clang but compiler already set to GCC.')


    def prefer_gcc(self, target_name):
        if not self.linux or self.raspi or self.gcc: return
        if not self.compiler_cmd:
            self.clang = False
            self.gcc   = True
            self.compiler_cmd = True
            if self.print:
                console(f'Target {target_name} requests GCC. Using GCC since no explicit compiler flag passed.')
        else:
            if self.print:
                console(f'Target {target_name} requested GCC but compiler already set to Clang.')


    ##
    # Enables fortran compiler
    # @path Optional custom path or command for the Fortran compiler
    #
    def enable_fortran(self, path=''):
        if self.fortran: return
        self.fortran = path if path else self.find_default_fortran_compiler()


    def find_compiler_root(self, suggested_path, compiler, suffixes, dumpfullversion) -> tuple[str, str, str]:
        """
        root path where the compilers exist and the discovered suffix
            returns (root_path, suffix, version)
        """
        def resolve_compiler(cxx_path, suffix) -> tuple[str, str, str]:
            cxx_path = os.path.realpath(cxx_path) # resolve symlinks
            if not os.path.exists(cxx_path):
                return '', '', ''
            version = self.get_gcc_clang_fullversion(cxx_path, dumpfullversion)
            return os.path.dirname(cxx_path) + '/', suffix, version

        # stop search early if we meet an already pre-configure /etc/alternatives/clang++ path on linux
        # since this is likely what the user has configured as their default compiler
        # if user has ~/.local/bin/clang or ~/.local/bin/gcc try to resolve that
        priority_choices = [ suggested_path, os.getenv('CXX'),
                            f'{os.getenv("HOME")}/.local/bin/{compiler}',
                            '/etc/alternatives/' + compiler ]
        for priority_cxx in priority_choices:
            if priority_cxx and os.path.exists(priority_cxx):
                path, _, ver = resolve_compiler(priority_cxx, '')
                if ver:
                    if self.verbose:
                        console(f'Compiler {compiler} ({ver}) at {os.path.realpath(priority_cxx)}')
                    return path, '', ver

        # perform exhaustive search through all candidate directories for any suitable compilers
        roots = []
        if suggested_path: roots.append(suggested_path)
        roots += ['/etc/alternatives/', '/usr/bin/', '/usr/local/bin/', '/bin/']

        # Look in PATH in addition to hardcoded paths
        pathDirs = os.getenv('PATH').split(":")
        pathDirs = list(map(lambda p: p if p.endswith("/") else p + "/", pathDirs)) # Add slash at end if missing
        roots += pathDirs

        candidates = []
        already_added = set()
        for root in roots:
            for suffix in suffixes:
                cxx_path = root + compiler + suffix # compiler=clang++
                if os.path.exists(cxx_path):
                    path, _, ver = resolve_compiler(cxx_path, suffix)
                    if ver and not path in already_added: # if version is valid and path not already added
                        already_added.add(path)
                        candidates.append((path, suffix, ver))
        if not candidates:
            raise EnvironmentError(f'Could not find {compiler} from {roots} with any suffix {suffixes}')

        def version_to_int(version_str):
            major_minor_patch = version_str.split('.')
            integer = 0
            for part in major_minor_patch:
                integer = integer*10 + int(part) if part else integer
            if integer == 0:
                console(f"Failed to check version for candidate='{version_str}'")
            return integer

        # sort by version, descending eg 10.3, 9.4, 8.3
        candidates.sort(key=lambda x: version_to_int(x[2]), reverse=True)

        # print this out for debugging on CI machines if they select verbose
        if self.verbose:
            for root, suffix, version in candidates:
                console(f'Compiler {compiler+suffix} ({version}) at {root+compiler+suffix}')

        root, suffix, version = candidates[0]
        if self.verbose:
            console(f'==> Selected {compiler+suffix} ({version}) at {root+compiler+suffix} <==')
        return root, suffix, version


    def get_preferred_compiler_paths(self):
        if self.cc_path and self.cxx_path and self.cxx_version:
            return (self.cc_path, self.cxx_path, self.cxx_version)

        # no preferred cc path for MSVC
        if self.msvc:
            return (self.cc_path, self.cxx_path, self.cxx_version)

        if self.android:
            self.cc_path  = self.android.cc_path()
            self.cxx_path = self.android.cxx_path()
            self.cxx_version = self.get_gcc_clang_fullversion(self.cc_path, dumpfullversion=False)
        elif self.yocto_linux:
            self.cc_path  = f'{self.yocto_linux.cc_prefix}gcc'
            self.cxx_path = f'{self.yocto_linux.cc_prefix}g++'
            self.cxx_version = self.get_gcc_clang_fullversion(self.cc_path, dumpfullversion=True)
        elif self.raspi:  # only GCC available for this platform
            ext = '.exe' if System.windows else ''
            self.cc_path  = f'{self.raspi_bin()}arm-linux-gnueabihf-gcc{ext}'
            self.cxx_path = f'{self.raspi_bin()}arm-linux-gnueabihf-g++{ext}'
            self.cxx_version = self.get_gcc_clang_fullversion(self.cc_path, dumpfullversion=True)
        elif self.mips:
            self.cc_path  = f'{self.mips.compiler_prefix()}gcc'
            self.cxx_path = f'{self.mips.compiler_prefix()}g++'
            self.cxx_version = self.get_gcc_clang_fullversion(self.cc_path, dumpfullversion=True)
        elif self.clang:
            suffixes = ['-20','-19','-18','-17','-16','-15','-14','-13','-12','-11','-10','-9','-8','-7','-6','']
            self.clang_path, suffix, ver = self.find_compiler_root(self.clang_path, 'clang++', suffixes, dumpfullversion=False)
            self.cc_path = f'{self.clang_path}clang{suffix}'
            self.cxx_path = f'{self.clang_path}clang++{suffix}'
            self.cxx_version = ver
        elif self.gcc:
            suffixes = ['-15','-14','-13','-12','-11','-10','-9','-8','-7','-6','']
            self.gcc_path, suffix, ver = self.find_compiler_root(self.gcc_path, 'g++', suffixes, dumpfullversion=True)
            self.cc_path = f'{self.gcc_path}gcc{suffix}'
            self.cxx_path = f'{self.gcc_path}g++{suffix}'
            self.cxx_version = ver

        if self.cc_path and self.cxx_path and self.cxx_version:
            return (self.cc_path, self.cxx_path, self.cxx_version)

        raise EnvironmentError('No preferred compiler for this platform!')


    def get_gcc_clang_fullversion(self, cc_path, dumpfullversion):
        if dumpfullversion:
            version = execute_piped([cc_path, '-dumpfullversion']).strip() # eg 9.4.0
            if version.count('.') >= 1:
                return version
        # clang++ doesn't support -dumpfullversion in latest releases -_-
        return execute_piped([cc_path, '-dumpversion']).strip()


    def compiler_version(self):
        if self.msvc:
            msvc_tools = self.get_msvc_tools_path()
            version = os.path.basename(msvc_tools.rstrip('\\//')).split('.')[0]
            return f'msvc{version}'
        elif self.macos:
            return self.macos_version
        elif self.ios:
            return self.ios_version
        elif self.linux or self.raspi or self.mips or self.android or self.yocto_linux:
            cc, _, version = self.get_preferred_compiler_paths()
            version_parts = version.split('.')
            major_version, minor_version = version_parts[0], version_parts[1]
            if 'gcc' in cc: return f'gcc{major_version}.{minor_version}'
            if 'clang' in cc: return f'clang{major_version}.{minor_version}'
            raise EnvironmentError(f'Unrecognized compiler {cc}!')
        else:
            raise EnvironmentError(f'Unknown compiler version!')


    def find_ninja_build(self):
        ninja_executables = [
            os.getenv('NINJA'),
            util.find_executable_from_system('ninja'),
            '/Projects/ninja.exe'
        ]
        for ninja_exe in ninja_executables:
            if ninja_exe and os.path.isfile(ninja_exe):
                if self.verbose: console(f'Found Ninja Build System: {ninja_exe}')
                return ninja_exe
        return ''


    def set_clang_tidy_path(self, clang_tidy_path=None):
        if not self.is_platform_set():
            console('Cannot set clang-tidy path since platform is not set yet!', color=Color.RED)
            return

        if clang_tidy_path and os.path.exists(clang_tidy_path):
            self.clang_tidy_path = clang_tidy_path
            if self.print: console(f'Using clang-tidy from {clang_tidy_path}', color=Color.GREEN)
            return

        CLANG_TIDY_ENV = 'CLANG_TIDY'
        if self.android:
            CLANG_TIDY_ENV = 'ANDROID_CLANG_TIDY'

        # respect user overrides first
        clang_tidy_env = os.getenv(CLANG_TIDY_ENV)
        if clang_tidy_env:
            if os.path.exists(clang_tidy_env):
                self.clang_tidy_path = clang_tidy_env
                if self.print: console(f'Using clang-tidy from {CLANG_TIDY_ENV} env: {clang_tidy_env}', color=Color.GREEN)
                return
            else:
                console(f'{CLANG_TIDY_ENV} environment variable is set to \'{clang_tidy_env}\' but it is not a valid file!', color=Color.YELLOW)

        # if android root has been configured, check if clang-tidy exists in the android toolchain bin dir
        if self.android:
            ndk_bin = self.android.bin()
            clang_tidy_exe = f'{ndk_bin}/clang-tidy.exe' if System.windows else f'{ndk_bin}/clang-tidy'
            if os.path.exists(clang_tidy_exe):
                self.clang_tidy_path = clang_tidy_exe
                if self.print: console(f'Found clang-tidy in Android NDK bin dir: {clang_tidy_exe}', color=Color.GREEN)
                return

        # display the full path of clang-tidy by resolving symlinks (/etc/alternatives/clang-tidy -> /usr/bin/clang-tidy-18)
        clang_tidy_exe = util.find_executable_from_system('clang-tidy', follow_symlinks=True)
        if clang_tidy_exe:
            self.clang_tidy_path = clang_tidy_exe
            if self.print: console(f'Found clang-tidy in PATH and resolved as: {clang_tidy_exe}', color=Color.GREEN)
            return

        self.clang_tidy_path = None
        console('clang-tidy not found! Static analysis will be disabled.', color=Color.YELLOW)
        console('install clang-tidy and add to PATH or define env CLANG_TIDY=<path>', color=Color.YELLOW)


    def add_sanitizer_option(self, option):
        if self.sanitize: self.sanitize += ',' + option
        else:             self.sanitize = option


    def add_coverage_option(self, option='default'):
        if self.coverage: self.coverage += ',' + option
        else:             self.coverage = option


    def append_env_path(self, paths, env):
        path = os.getenv(env)
        if path: paths.append(path)


    def raspi_bin(self):
        if not self.raspi_compilers: self.init_raspi_path()
        return self.raspi_compilers


    def raspi_sysroot(self):
        if not self.raspi_compilers: self.init_raspi_path()
        return self.raspi_system


    def raspi_includes(self):
        if not self.raspi_compilers: self.init_raspi_path()
        return self.raspi_include_paths


    def init_raspi_path(self):
        paths = []
        self.append_env_path(paths, 'RASPI_HOME')
        self.append_env_path(paths, 'RASPBERRY_HOME')
        if System.windows: paths += ['/SysGCC/raspberry']
        elif System.linux: paths += ['/usr/bin/raspberry', '/usr/local/bin/raspberry', '/opt/raspberry']
        compiler = ''
        if System.windows: compiler = 'bin/arm-linux-gnueabihf-gcc.exe'
        elif System.linux: compiler = 'arm-bcm2708/arm-linux-gnueabihf/bin/arm-linux-gnueabihf-gcc'
        for raspi_path in paths:
            if os.path.exists(f'{raspi_path}/{compiler}'):
                if not System.windows:
                    raspi_path = f'{raspi_path}/arm-bcm2708/arm-linux-gnueabihf/'
                self.raspi_compilers = f'{raspi_path}/bin/'
                self.raspi_system    = f'{raspi_path}/arm-linux-gnueabihf/sysroot'
                self.raspi_include_paths = [f'{raspi_path}/arm-linux-gnueabihf/lib/include']
                if self.print: console(f'Found RASPI TOOLS: {self.raspi_compilers}\n    sysroot: {self.raspi_system}')
                return
        raise EnvironmentError(f'''No Raspberry PI toolchain compilers detected!
Default search paths: {paths}
Define env RASPI_HOME with path to Raspberry tools.''')


    def set_android_toolchain(self, toolchain_file):
        """
        Sets the toolchain file for Android NDK.
        This should be abspath such as `/opt/android-sdk/ndk/25.2.9519653/build/cmake/android.toolchain.cmake`
        """
        self.android.set_toolchain_path(toolchain_file)


    def set_yocto_toolchain(self, toolchain_dir=None, toolchain_file=None):
        """
        For i.MX8M Plus, Xilinx, Oclea and other Yocto Linux based platforms.
        Sets the toolchain dir where these subdirs exist:
            aarch64-poky-linux/
            x86_64-pokysdk-linux/
        And optionally also sets the CMake toolchain file via `toolchain_file`.
        The `toolchain_file` is only used if `toolchain_dir` chosen as valid.
        """
        self.yocto_linux.init_toolchain(toolchain_dir, toolchain_file)


    def set_oclea_toolchain(self, toolchain_dir=None, toolchain_file=None):
        """
        Sets the toolchain dir where these subdirs exist:
            aarch64-oclea-linux/
            x86_64-ocleasdk-linux/
        And optionally also sets the CMake toolchain file via `toolchain_file`.
        The `toolchain_file` is only used if `toolchain_dir` chosen as valid.
        """
        self.yocto_linux.init_toolchain(toolchain_dir, toolchain_file)


    def set_imx8mp_toolchain(self, toolchain_dir=None, toolchain_file=None):
        """
        Sets the toolchain dir where these subdirs exist:
            aarch64-imx8mp-linux/
            x86_64-imx8mp-sdk-linux/
        And optionally also sets the CMake toolchain file via `toolchain_file`.
        The `toolchain_file` is only used if `toolchain_dir` chosen as valid.
        """
        self.yocto_linux.init_toolchain(toolchain_dir, toolchain_file)


    def set_xilinx_toolchain(self, toolchain_dir=None, toolchain_file=None):
        """
        Sets the toolchain dir where these subdirs exist:
            aarch64-xilinx-linux/
            x86_64-petalinux-linux/
        And optionally also sets the CMake toolchain file via `toolchain_file`.
        The `toolchain_file` is only used if `toolchain_dir` chosen as valid.
        """
        self.yocto_linux.init_toolchain(toolchain_dir, toolchain_file)


    def set_mips_toolchain(self, arch, toolchain_dir=None, toolchain_file=None):
        """
        Sets the toolchain dir for MIPS platform where at least the `bin` dir should exist
        And optionally also sets the CMake toolchain file via `toolchain_file`
        """
        self.mips.init_toolchain(arch, toolchain_dir, toolchain_file)


    def find_default_fortran_compiler(self):
        paths = []
        if System.linux:
            paths += [util.find_executable_from_system('gfortran')]

        for fortran_path in paths:
            if fortran_path and os.path.exists(fortran_path):
                if self.verbose: console(f'Found Fortran: {fortran_path}')
                return fortran_path
        return None


    def get_visualstudio_path(self):
        if self._visualstudio_path:
            return self._visualstudio_path
        if not System.windows:
            raise EnvironmentError('VisualStudio tools support not available on this platform!')

        vswhere_exe = "C:\\Program Files (x86)\\Microsoft Visual Studio\\Installer\\vswhere.exe"
        vspath = execute_piped(f'"{vswhere_exe}" -latest -nologo -property installationPath')
        if vspath and os.path.exists(vspath):
            self._visualstudio_path = vspath
            if self.verbose: console(f'Detected VisualStudio: {vspath}')
            return vspath

        paths = []
        vs_variants = [ 'Enterprise', 'Professional', 'Community'  ]
        for version in [ '18', '2022' ]: # new 64-bit VS
            for variant in vs_variants:
                paths.append(f'C:\\Program Files\\Microsoft Visual Studio\\{version}\\{variant}')
        for version in [ '2019', '2017' ]:
            for variant in vs_variants:
                paths.append(f'C:\\Program Files (x86)\\Microsoft Visual Studio\\{version}\\{variant}')

        for path in paths:
            if path and os.path.exists(path):
                self._visualstudio_path = path
                if self.verbose: console(f'Detected VisualStudio: {path}')
                return path

        return self._visualstudio_path


    def is_target_arch_x64(self): return self.arch == 'x64'
    def is_target_arch_x86(self): return self.arch == 'x86'
    def is_target_arch_arm64(self): return self.arch == 'arm64'
    def is_target_arch_armv7(self): return self.arch == 'arm'


    def get_gcc_linux_march(self):
        if self.is_target_arch_arm64(): return 'native' if System.aarch64 else 'armv8-a'
        if self.is_target_arch_x64(): return 'native' if System.x86_64 else 'x86-64'
        if self.is_target_arch_x86(): return 'native' if System.x86 else 'pentium4'
        raise RuntimeError(f'Unsupported arch: {self.arch}')


    def get_visualstudio_cmake_arch(self):
        if self.is_target_arch_x64(): return 'x64'
        if self.is_target_arch_x86(): return 'Win32'
        if self.arch == 'arm':   return 'ARM'
        if self.arch == 'arm64': return 'ARM64'
        raise RuntimeError(f'Unsupported arch: {self.arch}')


    def get_visualstudio_cmake_id(self):
        if self._visualstudio_cmake_id:
            return self._visualstudio_cmake_id

        path = self.get_visualstudio_path()
        if '\\2022\\' in path: self._visualstudio_cmake_id = 'Visual Studio 17 2022'
        elif '\\18\\' in path: self._visualstudio_cmake_id = 'Visual Studio 18 2026'
        elif '\\2019\\' in path: self._visualstudio_cmake_id = 'Visual Studio 16 2019'
        else:                  self._visualstudio_cmake_id = 'Visual Studio 15 2017'

        if self.verbose: console(f'Detected CMake Generator: -G"{self._visualstudio_cmake_id}" -A {self.get_visualstudio_cmake_arch()}')
        return self._visualstudio_cmake_id


    def get_msbuild_path(self):
        if self._msbuild_path:
            return self._msbuild_path

        paths = [ util.find_executable_from_system('msbuild') ]
        if System.windows:
            vswhere = '"C:\\Program Files (x86)\\Microsoft Visual Studio\\Installer\\vswhere.exe" -latest -nologo -property installationPath'
            paths.append(f"{execute_piped(vswhere)}\\MSBuild\\Current\\Bin\\MSBuild.exe")
            paths.append(f"{execute_piped(vswhere)}\\MSBuild\\15.0\\Bin\\amd64\\MSBuild.exe")

            vs_variants = [ 'Enterprise', 'Professional', 'Community' ]
            for variant in vs_variants:
                paths.append(f'C:\\Program Files (x86)\\Microsoft Visual Studio\\2019\\{variant}\\MSBuild\\Current\\Bin\\MSBuild.exe')
                paths.append(f'C:\\Program Files (x86)\\Microsoft Visual Studio\\2017\\{variant}\\MSBuild\\15.0\\Bin\\amd64\\MSBuild.exe')

        for path in paths:
            if path and os.path.exists(path):
                self._msbuild_path = path
                if self.verbose: console(f'Detected MSBuild: {path}')
                return path
        raise EnvironmentError('Failed to find MSBuild from system PATH. You can easily configure msbuild by running `mama install-msbuild`.')


    ## MSVC tools at, for example: "{VisualStudioPath}\VC\Tools\MSVC\14.16.27023"
    def get_msvc_tools_path(self):
        if self._msvctools_path:
            return self._msvctools_path
        if not System.windows:
            raise EnvironmentError('MSVC tools not available on this platform!')

        tools_root = f"{self.get_visualstudio_path()}\\VC\\Tools\\MSVC"
        tools = os.listdir(tools_root)
        if not tools:
            raise EnvironmentError('Could not detect MSVC Tools')

        tools_path = os.path.join(tools_root, tools[0])
        #tools_path = forward_slashes(tools_path)
        self._msvctools_path = tools_path
        if self.verbose: console(f'Detected MSVC Tools: {tools_path}')
        return tools_path


    def get_msvc_bin64(self):
        return f'{self.get_msvc_tools_path()}/bin/Hostx64/x64/'


    def get_msvc_link64(self):
        return f'{self.get_msvc_bin64()}link.exe'


    def get_msvc_cl64(self):
        return f'{self.get_msvc_bin64()}cl.exe'


    def get_msvc_lib64(self):
        return f'{self.get_msvc_tools_path()}\\lib\\x64'


    def install_clang(self, clang_major):
        if type(clang_major) != int: clang_major = int(clang_major) # convert to int
        if System.windows: raise OSError('Install Visual Studio 2026 with Clang support')
        if System.macos:   raise OSError('Install Xcode to get Clang on macOS')
        id, major, minor = self.get_distro_info()
        if id != "ubuntu": raise OSError(f'install-clang-{clang_major} only supports ubuntu')
        console(f'Installing clang-{clang_major} and libc++-{clang_major}-dev from apt repositories', color=Color.MAGENTA)
        execute('sudo apt-get update')
        execute(f'sudo apt-get install clang-{clang_major} clang-tidy-{clang_major} '+\
                f'libc++-{clang_major}-dev libc++abi-{clang_major}-dev -y')
        # configure current clang version as default clang via update-alternatives
        # this way mama and cmake tools can find it without additional configuration
        console(f'Configuring clang-{clang_major} as default clang via update-alternatives', color=Color.MAGENTA)
        execute(f'sudo update-alternatives --install /usr/bin/clang   clang   /usr/bin/clang-{clang_major}   100')
        execute(f'sudo update-alternatives --install /usr/bin/clang++ clang++ /usr/bin/clang++-{clang_major} 100')
        execute(f'sudo update-alternatives --install /usr/bin/clang-tidy clang-tidy /usr/bin/clang-tidy-{clang_major} 100')
        execute(f'sudo update-alternatives --install /usr/bin/run-clang-tidy run-clang-tidy /usr/lib/llvm-{clang_major}/bin/run-clang-tidy 100')
        execute(f'sudo update-alternatives --set clang   /usr/bin/clang-{clang_major}')
        execute(f'sudo update-alternatives --set clang++ /usr/bin/clang++-{clang_major}')
        execute(f'sudo update-alternatives --set clang-tidy /usr/bin/clang-tidy-{clang_major}')
        execute(f'sudo update-alternatives --set run-clang-tidy /usr/lib/llvm-{clang_major}/bin/run-clang-tidy')


    def install_gcc(self, gcc_major):
        if type(gcc_major) != int: gcc_major = int(gcc_major) # convert to int
        if System.windows: raise OSError('Install MinGW to get GCC on Windows')
        if System.macos:   raise OSError('install-gcc not implemented for macOS')
        id, major, minor = self.get_distro_info()
        if id != "ubuntu": raise OSError(f'install-gcc-{gcc_major} only supports ubuntu')
        console(f'Installing gcc-{gcc_major} and g++-{gcc_major} from apt repositories', color=Color.MAGENTA)
        execute('sudo apt-get update')
        execute(f'sudo apt-get install gcc-{gcc_major} g++-{gcc_major} -y')
        # configure current gcc version as default gcc via update-alternatives
        # this way mama and cmake tools can find it without additional configuration
        console(f'Configuring gcc-{gcc_major} as default gcc via update-alternatives', color=Color.MAGENTA)
        execute(f'sudo update-alternatives --install /usr/bin/gcc gcc /usr/bin/gcc-{gcc_major} 100')
        execute(f'sudo update-alternatives --install /usr/bin/g++ g++ /usr/bin/g++-{gcc_major} 100')
        execute(f'sudo update-alternatives --set gcc /usr/bin/gcc-{gcc_major}')
        execute(f'sudo update-alternatives --set g++ /usr/bin/g++-{gcc_major}')


    def install_msbuild(self):
        if System.windows: raise OSError('Install Visual Studio 2019 to get MSBuild on Windows')
        if System.macos:   raise OSError('install_msbuild not implemented for macOS')

        id, _, _ = self.get_distro_info()
        if id != "ubuntu": raise OSError('install_msbuild only supports ubuntu')
        codename = distro.info()['codename']

        execute('curl https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor > /tmp/microsoft.gpg')
        execute('sudo mv /tmp/microsoft.gpg /etc/apt/trusted.gpg.d/microsoft.gpg')
        execute(f"sudo sh -c 'echo \"deb [arch=amd64] https://packages.microsoft.com/repos/microsoft-ubuntu-{codename}-prod {codename} main\" > /etc/apt/sources.list.d/dotnetdev.list'")
        execute('sudo apt-get install apt-transport-https')
        execute('sudo apt-get update')
        execute('sudo apt-get install dotnet-sdk-2.1')


    def install_ndk(self, ndk_key):
        ndk_versions = {
            'r25c': { 'ver': '25.2.9519653', 'url': 'https://dl.google.com/android/repository/android-ndk-r25c-linux.zip' },
            'r26d': { 'ver': '26.3.11579264', 'url': 'https://dl.google.com/android/repository/android-ndk-r26d-linux.zip' },
            'r27d': { 'ver': '27.3.13750724', 'url': 'https://dl.google.com/android/repository/android-ndk-r27d-linux.zip' },
            'r28c': { 'ver': '28.2.13676358', 'url': 'https://dl.google.com/android/repository/android-ndk-r28c-linux.zip' },
            'r29':  { 'ver': '29.0.14206865', 'url': 'https://dl.google.com/android/repository/android-ndk-r29-linux.zip' },
            'r30':  { 'ver': '30.0.14608247', 'url': 'https://dl.google.com/android/repository/android-ndk-r30-beta1-linux.zip' },
        }

        if not ndk_key.startswith('r'):
            ndk_key = 'r' + ndk_key # add 'r' prefix if missing, eg 25c -> r25c

        if not ndk_key in ndk_versions:
            supported = '\n  '.join([f'{key} ({ndk_versions[key]["ver"]})' for key in ndk_versions.keys()])
            raise ValueError(f'Unsupported NDK version: {ndk_key}. Supported versions are:\n  {supported}')

        ndk_version = ndk_versions[ndk_key]['ver']
        ndk_url = ndk_versions[ndk_key]['url']
        if System.windows:
            ndk_url = ndk_url.replace('-linux.zip', '-windows.zip')
        elif System.macos:
            ndk_url = ndk_url.replace('-linux.zip', '-darwin.dmg')

        if System.macos:
            ndk_dest = f'{os.getenv("HOME")}/Library/Android/sdk/ndk'
        elif System.windows:
            ndk_dest = f'{os.getenv("LOCALAPPDATA")}\\Android\\Sdk\\ndk'
        elif System.linux:
            ndk_dest = f'/opt/android-sdk/ndk'

        console(f'Downloading NDK {ndk_version}')
        ndk_zip = util.download_file(ndk_url, tempfile.gettempdir())

        if System.windows or System.macos:
            os.makedirs(ndk_dest, exist_ok=True)
        else:
            execute(f'sudo mkdir -p {ndk_dest} && sudo chown -R $USER {ndk_dest}')

        console(f'Extracting NDK to {ndk_dest}/{ndk_version}')
        util.unzip(ndk_zip, ndk_dest)

        final_dest = f'{ndk_dest}/{ndk_version}'
        if os.path.exists(final_dest):
            shutil.rmtree(final_dest)

        shutil.move(f'{ndk_dest}/android-ndk-{ndk_key}', final_dest)
        if os.path.exists(f'{final_dest}/build'):
            console(f'NDK installed successfully to {final_dest}')
        else:
            raise RuntimeError(f'Failed to install NDK to {final_dest}')

        console(f'Adding ANDROID_NDK_HOME={final_dest} to ~/.bashrc, run source ~/.bashrc or restart terminal to populate your env.')
        # remove existing ANDROID_NDK_HOME from bashrc if exists
        execute('sed -i "/export ANDROID_NDK_HOME/d" ~/.bashrc')
        # add new ANDROID_NDK_HOME to bashrc
        execute(f'echo "export ANDROID_NDK_HOME={final_dest}" >> ~/.bashrc')


    def run_convenient_installs(self):
        for tool in self.convenient_install:
            if 'clang-' in tool: self.install_clang(tool[6:])
            elif 'gcc-' in tool: self.install_gcc(tool[4:])
            elif 'msbuild' in tool: self.install_msbuild()
            elif 'ndk-'    in tool: self.install_ndk(tool[4:])


    def libname(self, library):
        if self.msvc: return f'{library}.lib'
        else:         return f'lib{library}.a'


    def libext(self):
        return 'lib' if self.msvc else 'a'


    def has_target(self) -> bool:
        """ A target was specified from cmdline, eg 'all' or 'mypackage' """
        return self.target is not None and len(self.target) > 0

    def no_target(self) -> bool:
        """ No target specified from cmdline """
        return self.target is None or len(self.target) == 0

    def targets_all(self) -> bool:
        """ Target specified from cmdline was 'all' """
        return self.target == 'all'


    def target_matches(self, target_name: str) -> bool:
        """ True if target_name matches the target specified from cmdline """
        return self.targets_all() \
            or (self.target and self.target.lower() == target_name.lower())


    def no_specific_target(self) -> bool:
        """ True if no target or 'all' was specified from cmdline """
        return self.no_target() or self.targets_all()

