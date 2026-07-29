from __future__ import annotations
import os, sys, tempfile, psutil, shutil, threading, time
from typing import List, TYPE_CHECKING
from mama.platforms.oclea import Oclea
from mama.platforms.xilinx import Xilinx
from mama.platforms.mips import Mips
from mama.platforms.android import Android
from mama.platforms.imx8mp import Imx8mp
from mama.platforms.generic_yocto import GenericYocto
from mama.platforms.raspi import Raspi, triple_for_arch
from mama.platforms.ios import Ios
from mama.platforms.windows import Windows
from mama.platforms.linux import Linux
from mama.platforms.macos import Macos
from mama.platforms.platform import Platform
from mama.platforms.registry import platform_for_arg
import mama.util as util
from .utils.system import System, console, Color, warning
from .utils.sub_process import execute, execute_piped

if System.linux:
    import distro

if TYPE_CHECKING:
    from .build_dependency import BuildDependency

class UpdateStats:
    """Counts and times clone/pull/shim-fetch activity during the load phase."""
    def __init__(self):
        self._lock = threading.Lock()
        self.cloned = 0
        self.pulled = 0
        self.shim_fetched = 0
        self._start = None
        self._duration = 0.0

    def start(self):
        self._start = time.monotonic()

    def stop(self):
        if self._start is not None:
            self._duration = time.monotonic() - self._start
            self._start = None

    def record_clone(self):
        with self._lock: self.cloned += 1

    def record_pull(self):
        with self._lock: self.pulled += 1

    def record_shim(self):
        with self._lock: self.shim_fetched += 1

    @property
    def total(self) -> int:
        return self.cloned + self.pulled + self.shim_fetched

    @property
    def duration(self) -> float:
        return self._duration

    def summary_line(self) -> str:
        """One-line summary, or '' if nothing happened."""
        if self.total == 0:
            return ''
        parts = []
        if self.shim_fetched: parts.append(f'{self.shim_fetched} shim-fetched')
        if self.pulled:       parts.append(f'{self.pulled} pulled')
        if self.cloned:       parts.append(f'{self.cloned} cloned')
        # Local import to avoid circular dependency with util
        from .util import get_time_str
        return f'Updated {self.total} target(s): {", ".join(parts)} in {get_time_str(self._duration)}'


class BuildConfig:
    """
    Mama Build Configuration is created only once in the root project working directory.
    This configuration is then passed down to dependencies.
    """
    @staticmethod
    def _default_build_jobs() -> int:
        """Default parallel build jobs. Linux leaves ONE core free so a perfectly-parallel build can't
        saturate the desktop into an OOM/freeze; Windows/macOS use all cores. An explicit `jobs=N`
        on the command line overrides this."""
        cpu = psutil.cpu_count() or 4
        return max(1, cpu - 1) if System.linux else cpu

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
        self.sched_debug = False # TEMP: print each target's build-weight calc, then exit without building
        self.buildstats = False # after the build, print a per-package load/configure/build time breakdown
        self.unshallow = False  # by default, git clones are shallow, this allows unshallowing
        self.git_url_override = None  # 'https' or 'ssh': rewrite add_git() urls at build time
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
        self.update_stats = UpdateStats() # clone/pull/shim counters for the load phase summary
        self.enable_clang_tidy = False # enables clang-tidy static analysis during build
        self.clang_tidy_path = None # resolved path to clang-tidy executable
        # The ONE active platform. set_platform() installs it and derives the mamafile-facing
        # flags below from it, so nothing else stores platform state.
        self.platform : Platform = None
        self.msvc    = False # whether this is a MSVC build on Windows
        self.linux   = False
        self.macos   = False
        self.ios     : Ios = None
        self.android : Android = None
        self.raspi   : Raspi = None
        self.mips    : Mips = None
        self.oclea   : Oclea = None
        self.xilinx  : Xilinx = None
        self.imx8mp  : Imx8mp = None
        self.yocto_linux : GenericYocto = None # any generic Yocto Linux board (Oclea, Xilinx, IMX8MP)
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
        self.compiler_conflict_warned = False  # the "target prefers X but compiler locked to Y" note fires once, not per dep
        self.clang_stdlib = 'libc++'  # linux clang C++ stdlib; see use_gcc_stdlib_for_clang()
        self.fortran = ''
        # build optimization
        self.release = True
        self.debug   = False
        # valid architectures: x86, x64, arm, arm64
        self.arch    = None
        self.distro  = None  # distro information (name, major, minor)
        self.jobs    = BuildConfig._default_build_jobs()
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
        ## Convenient installation utils:
        self.convenient_install = []
        ## Workspace and parsing
        self.parallel_load = False  ## Whether to load dependencies in parallel?
        self.serial_load   = False  ## If True, override the auto-parallel-on-update behaviour
        self.parallel_max  = 20     ## Cap concurrent git fetches (avoids hammering the SSH master)
        # The toolchain file a platform picked, '' for a native build. cmake_configure records it and
        # reads it back: a toolchain file owns compiler selection, so mama must not name a compiler too.
        self.cmake_toolchain_file = ''
        self.git_timeout   = 30     ## Kill a git clone/fetch with no progress for this many seconds
        self.no_compiler_cache = False  ## Disable cross-build-dir reuse of cmake compiler detection
        self.global_workspace = False
        # The root project dir (mamabuild sets it from source_dir); cwd for a `mama <host> build` bootstrap
        # child so it resolves the same dependency graph. None until mamabuild runs (direct-construct tests).
        self.root_source_dir = None
        if System.windows:
            self.workspaces_root = util.normalized_path(os.getenv('HOMEPATH'))
        else:
            self.workspaces_root = os.getenv('HOME')
        self._network_available = None  # None=untested, True/False=result
        self._announced = set()          # announce_once() keys already printed
        self._announce_lock = threading.Lock()
        self._cmake_ver_num = None   # cached `cmake --version`, also the CMakeFiles/<ver> dir name
        self._seed_coord = None      # compiler-seed Coordinator, built on first configure
        self._buildstats_start = None  # buildstats wall start; set only on a non-MSVC insights run
        self._timetrace_json = None    # vcperf trace path; set only on an MSVC insights run
        self.unused_args = []
        self.loaded_dependencies : dict[str, BuildDependency] = {}
        self.dep_registry_lock = threading.Lock()  # guards loaded_dependencies under parallel_load
        self.parse_args(args)
        self.check_platform()
        if self.buildstats and self.clang:
            self.run_cmake_configure = True  # Linux/Clang: the -ftime-trace compile flag must be (re)applied


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
            elif arg == 'https-override': self.git_url_override = 'https'
            elif arg == 'ssh-override':   self.git_url_override = 'ssh'
            elif arg == 'sched_debug': self.sched_debug = True  # TEMP: print build-weight calc, no build
            elif arg == 'buildstats':  self.buildstats = True   # print per-package timing breakdown after build
            elif arg == 'configure':
                self.run_cmake_configure = True
                self.build = True # configure implies a build
            elif arg == 'init':      self.mama_init = True
            elif arg == 'silent':    self.print = False
            elif arg == 'verbose':   self.verbose = True
            elif arg == 'parallel':  self.parallel_load = True
            elif arg == 'serial':    self.serial_load = True
            elif arg == 'nocache' or arg == 'no-compiler-cache': self.no_compiler_cache = True
            elif arg.startswith('parallel_max='):
                try: self.parallel_max = max(1, int(arg.split('=', 1)[1]))
                except (ValueError, IndexError): pass
            elif arg.startswith('git_timeout='):
                try: self.git_timeout = max(5, int(arg.split('=', 1)[1]))
                except (ValueError, IndexError): pass
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
            elif platform_for_arg(arg): self.select_platform_arg(arg)
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
            elif arg == 'install-raspi':   self.convenient_install.append('raspi-arm64')
            elif arg == 'install-raspi32': self.convenient_install.append('raspi-arm')
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


    ## set_platform() flag to platform class name, in resolution order. Only the FIRST enabled flag
    ## wins, exactly as the old if/elif chain did. Looked up through this module's globals, so a test
    ## can swap a platform out with monkeypatch.setattr('mama.build_config.Imx8mp', Fake).
    _PLATFORM_FLAGS = (('msvc','Windows'), ('linux','Linux'), ('macos','Macos'), ('ios','Ios'),
                       ('android','Android'), ('raspi','Raspi'), ('oclea','Oclea'), ('mips','Mips'),
                       ('xilinx','Xilinx'), ('imx8mp','Imx8mp'))

    def set_platform(self, **flags) -> bool:
        """Select the ONE active platform by flag, eg set_platform(android=True). The first enabled
        flag wins. All-False clears the platform."""
        for flag, class_name in BuildConfig._PLATFORM_FLAGS:
            if flags.get(flag):
                self.set_platform_class(globals()[class_name])
                return True
        self.set_platform_class(None)
        return True


    def select_platform_arg(self, arg: str):
        """Select the platform a CLI arg names, eg `raspi32` -> Raspi pinned to arm."""
        cls, arch = platform_for_arg(arg)
        self.set_platform_class(cls)
        if arch: self.set_arch(arch)


    def set_platform_class(self, cls):
        """Install `cls` as the one active platform. Selecting the SAME platform twice keeps the
        existing instance, so `mama build android-31 ndk-28` does not throw away the api level."""
        if cls is None:                     self.platform = None
        elif type(self.platform) is not cls: self.platform = cls(self)
        self._update_platform_flags()


    def _update_platform_flags(self):
        """Refresh the per-platform mamafile properties from `self.platform`. `msvc`, `linux` and
        `macos` stay bools. The rest hand back the platform object, because a mamafile reads
        `config.android.android_api` off them. Both forms are documented API - see README."""
        p = self.platform
        def obj(cls): return p if isinstance(p, cls) else None
        self.msvc  = isinstance(p, Windows)
        self.linux = isinstance(p, Linux)
        self.macos = isinstance(p, Macos)
        self.ios     = obj(Ios)
        self.android = obj(Android)
        self.raspi   = obj(Raspi)
        self.oclea   = obj(Oclea)
        self.mips    = obj(Mips)
        self.xilinx  = obj(Xilinx)
        self.imx8mp  = obj(Imx8mp)
        # convenience alias for detecting embedded Yocto Linux platforms (e.g. Oclea, Xilinx, IMX8MP)
        self.yocto_linux = obj(GenericYocto)


    def is_platform_set(self):
        return self.platform is not None


    def check_platform(self):
        if not self.is_platform_set():
            # choose MSVC if user did not specify `mama build gcc` on windows system
            msvc = System.windows and not (self.gcc or self.clang)
            self.set_platform(msvc=msvc, linux=System.linux, macos=System.macos)
            if not self.is_platform_set():
                raise RuntimeError(f'Unsupported platform {sys.platform}: Please specify platform!')
        if not self.msvc and not (self.gcc or self.clang):
            self.gcc = True # default to GCC on non-MSVC platforms

        if not self.arch: self.set_arch(self.platform.get_default_arch())
        self.platform.validate_arch(self.arch) # set_arch() only checks the arch name itself

        if self.enable_clang_tidy:
            # resolve clang-tidy path based on platform
            self.set_clang_tidy_path(self.clang_tidy_path)


    def get_distro_info(self):
        if not self.distro:
            self.distro = self.platform.distro_version()
        return self.distro


    def set_arch(self, arch):
        arches = ['x86', 'x64', 'arm', 'arm64', 'mips', 'mipsel', 'mips64', 'mips64el']
        if not arch in arches:
            raise RuntimeError(f"Unrecognized architecture {arch}! Valid options are: {arches}")
        self.arch = arch


    def is_64bit_build(self):
        return (self.arch == 'x64' or self.arch == 'arm64')


    def name(self):
        return self.platform.name if self.platform else 'build'


    def host_platform_name(self):
        """Build-dir / CLI platform name of the HOST mama runs on ('windows'|'linux'|'macos'), independent of
        the (possibly cross-compiled) target platform. The single source of truth for locating host build dirs
        and forming a `mama <host> build` bootstrap; mirrors name() for the host. Host arch is left out (as the
        `windows`/`linux` build-dir names are for x64), which matches every non-arm64-host build."""
        if System.windows: return 'windows'
        if System.macos:   return 'macos'
        return 'linux'


    # per-sanitizer build dir suffix so flavors don't share a dir and force a reconfigure
    SANITIZER_SUFFIX = { 'address':'-asan', 'leak':'-lsan', 'thread':'-tsan', 'undefined':'-ubsan' }

    def compiler_build_dir_suffix(self):
        """'-clang' on linux clang builds, else ''. Shared dir = one compiler clobbers the other, then g++
        links libc++ archives. gcc keeps bare 'linux' (no churn); elsewhere the toolset/SDK fixes the compiler."""
        return '-clang' if (self.linux and self.clang) else ''

    def build_dir_with_suffix(self, build_dir):
        build_dir += self.compiler_build_dir_suffix()  # coarsest axis first: linux-clang-coverage-tsan
        if self.coverage: build_dir += '-coverage'
        if self.sanitize: build_dir += ''.join(self.SANITIZER_SUFFIX.get(s, '-'+s) for s in self.sanitize.split(','))
        return build_dir


    def platform_build_dir_name(self):
        """
        Gets the build folder name depending on platform and architecture.
        By default 64-bit architectures use the platform name, eg 'windows' or 'linux'
        And 32-bit architectures add a suffix, eg 'windows32' or 'linux32'
        Coverage builds add '-coverage' and sanitizer builds add a further
        suffix, eg 'linux-coverage', 'linux-asan' or 'linux-coverage-asan'.
        """
        return self.build_dir_with_suffix(self._platform_build_dir_name())


    def _platform_build_dir_name(self):
        return self.platform.build_dir_name() if self.platform else 'build'


    def set_build_config(self, release=False, debug=False):
        self.release = release
        self.debug   = debug
        return True


    def set_artifactory_ftp(self, ftp_url: str, auth='store'):
        """ @see BuildTarget.set_artifactory_ftp() for documentation """
        self.artifactory_ftp = ftp_url
        self.artifactory_auth = auth


    def announce_once(self, key: str, text: str):
        """Print `text` only the first time `key` comes up. Option builders run per fingerprint
        computation, not per configure, so a plain console() repeats the same line with no new news."""
        if not self.print: return
        with self._announce_lock:
            if key in self._announced: return
            self._announced.add(key)
        console(text)


    def clean_only(self) -> bool:
        """`clean` with no build: nothing to fetch, configure or package afterwards - just delete."""
        return self.clean and not self.build


    def lock_compiler(self):
        """Freeze the compiler after the ROOT mamafile's settings(). build_dir depends on it, so a dep
        flipping it mid-load would scatter the tree across linux/ and linux-clang/. Later prefer_*() just notes."""
        self.compiler_cmd = True


    def _warn_compiler_conflict(self, target_name, requested, locked):
        """The 'target prefers X but the compiler is already locked to Y' note - emitted ONCE per run
        (every dep re-requests its preference, so this used to flood the output one line per target)."""
        if self.print and not self.compiler_conflict_warned:
            self.compiler_conflict_warned = True
            console(f'Target {target_name} requested {requested} but compiler already set to {locked}.')


    def prefer_clang(self, target_name):
        if not self.linux or self.raspi or self.clang: return
        if not self.compiler_cmd:
            self.clang = True
            self.gcc   = False
            self.compiler_cmd = True
            if self.print:
                console(f'Target {target_name} requests Clang. Using Clang since no explicit compiler flag passed.')
        else:
            self._warn_compiler_conflict(target_name, 'Clang', 'GCC')


    def prefer_gcc(self, target_name):
        if not self.linux or self.raspi or self.gcc: return
        if not self.compiler_cmd:
            self.clang = False
            self.gcc   = True
            self.compiler_cmd = True
            if self.print:
                console(f'Target {target_name} requests GCC. Using GCC since no explicit compiler flag passed.')
        else:
            self._warn_compiler_conflict(target_name, 'GCC', 'Clang')


    def use_gcc_stdlib_for_clang(self):
        """Use libstdc++ instead of libc++ for linux clang, to link GNU-built prebuilts like Qt.
           Call from the root mamafile settings() so it applies to every target."""
        self.clang_stdlib = 'libstdc++'


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
                        console(f'Compiler {compiler} ({ver}) at {os.path.realpath(priority_cxx)} via {priority_cxx}')
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

        # a cross platform always names its own compilers: falling back to the host compiler
        # search would silently build x86 binaries for an arm64 board
        cc, cxx, ver = self.platform.compiler_paths()
        if cc:
            self.cc_path, self.cxx_path, self.cxx_version = cc, cxx, ver
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
        return self.platform.compiler_version_tag()


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
                warning(f'{CLANG_TIDY_ENV} environment variable is set to \'{clang_tidy_env}\' but it is not a valid file!')

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
        warning('clang-tidy not found! Static analysis will be disabled.')
        warning('install clang-tidy and add to PATH or define env CLANG_TIDY=<path>')


    def add_sanitizer_option(self, option):
        if self.sanitize: self.sanitize += ',' + option
        else:             self.sanitize = option


    # asan/tsan/ubsan/lsan runtimes are mutually incompatible (asan vs tsan
    # cannot link together; ubsan combos vary). Package archives built with
    # different sanitizers must therefore have distinct names so a tsan build
    # isn't downloaded into an asan consumer.
    _SANITIZER_SHORT_NAMES = {
        'address':   'asan',
        'thread':    'tsan',
        'leak':      'lsan',
        'undefined': 'ubsan',
        'memory':    'msan',
    }

    def sanitizer_suffix(self):
        """Short package-name suffix for the active sanitizer config:
        'asan', 'tsan', 'asan_ubsan', etc. Returns '' if no sanitizer is set.
        Multiple sanitizers are joined with '_' to keep '-' as the field
        separator in the surrounding archive name."""
        if not self.sanitize:
            return ''
        parts = []
        for s in self.sanitize.split(','):
            s = s.strip()
            if s:
                parts.append(BuildConfig._SANITIZER_SHORT_NAMES.get(s, s))
        return '_'.join(parts)


    def add_coverage_option(self, option='default'):
        if self.coverage: self.coverage += ',' + option
        else:             self.coverage = option


    def append_env_path(self, paths, env):
        path = os.getenv(env)
        if path: paths.append(path)


    def set_toolchain(self, toolchain_dir=None, toolchain_file=None):
        """Point the active platform at an explicit toolchain, from the ROOT mamafile settings().

        `toolchain_dir` is the SDK root holding the cross compilers and the sysroot. `toolchain_file`
        is the CMake toolchain file the SDK ships. A platform uses whichever of the two it needs.
        ```
            def settings(self):
                self.config.set_toolchain('/opt/my-imx8mp-sdk')
        ```
        """
        self.platform.init_toolchain(toolchain_dir, toolchain_file)


    ## Platform-named aliases of set_toolchain(), kept because mamafiles and the README use them.
    def set_yocto_toolchain(self, toolchain_dir=None, toolchain_file=None):
        """ i.MX8M Plus, Xilinx, Oclea and other Yocto Linux boards. @see set_toolchain() """
        self.set_toolchain(toolchain_dir, toolchain_file)

    def set_oclea_toolchain(self, toolchain_dir=None, toolchain_file=None):
        """ Ambarella CV25 by Oclea. @see set_toolchain() """
        self.set_toolchain(toolchain_dir, toolchain_file)

    def set_imx8mp_toolchain(self, toolchain_dir=None, toolchain_file=None):
        """ NXP i.MX8M Plus. @see set_toolchain() """
        self.set_toolchain(toolchain_dir, toolchain_file)

    def set_xilinx_toolchain(self, toolchain_dir=None, toolchain_file=None):
        """ Xilinx Zynq UltraScale+ MPSoC. @see set_toolchain() """
        self.set_toolchain(toolchain_dir, toolchain_file)

    def set_android_toolchain(self, toolchain_file):
        """ Android NDK, eg `/opt/android-sdk/ndk/25.2.9519653/build/cmake/android.toolchain.cmake` """
        self.android.set_toolchain_path(toolchain_file)

    def set_mips_toolchain(self, arch, toolchain_dir=None, toolchain_file=None):
        """ MIPS, whose toolchain dir must hold a bin/ subdir. @see set_toolchain() """
        self.mips.init_toolchain(toolchain_dir, toolchain_file, arch)


    def init_platform_toolchain(self):
        """Resolve the cross-compile toolchain (NDK/sysroot paths) from the default search paths.
        MUST run after the ROOT mamafile's settings(), so an explicit set_toolchain() there wins:
        this is a no-op once settings() already picked a toolchain dir."""
        self.platform.init_default()


    def find_default_fortran_compiler(self):
        paths = []
        if System.linux:
            paths += [util.find_executable_from_system('gfortran')]

        for fortran_path in paths:
            if fortran_path and os.path.exists(fortran_path):
                if self.verbose: console(f'Found Fortran: {fortran_path}')
                return fortran_path
        return None


    def is_target_arch_x64(self): return self.arch == 'x64'
    def is_target_arch_x86(self): return self.arch == 'x86'
    def is_target_arch_arm64(self): return self.arch == 'arm64'
    def is_target_arch_armv7(self): return self.arch == 'arm'


    ## MSVC paths a mamafile links against. The Windows platform owns the discovery behind them.
    def get_visualstudio_path(self): return self.platform.visualstudio_path()
    def get_msvc_tools_path(self):   return self.platform.msvc_tools_path()
    def get_msvc_bin64(self):        return self.platform.msvc_bin64()
    def get_msvc_cl64(self):         return self.platform.msvc_cl64()
    def get_msvc_link64(self):       return self.platform.msvc_link64()
    def get_msvc_lib64(self):        return self.platform.msvc_lib64()


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


    def install_raspi(self, arch: str):
        """Install the Raspberry Pi cross toolchain from apt. The packages land in /usr/bin/<triple>-gcc,
        which Raspi.init_default() already searches, so a build works straight afterwards with no env var."""
        if System.windows: raise OSError('install-raspi is linux only. On Windows install SysGCC/raspberry')
        if System.macos:   raise OSError('install-raspi not implemented for macOS')
        id, _, _ = self.get_distro_info()
        if id not in ('ubuntu', 'debian'): raise OSError(f'install-raspi only supports ubuntu/debian, not {id}')

        triple = triple_for_arch(arch)
        console(f'Installing {triple} cross toolchain from apt repositories', color=Color.MAGENTA)
        execute('sudo apt-get update')
        execute(f'sudo apt-get install -y gcc-{triple} g++-{triple}')

        gcc = f'/usr/bin/{triple}-gcc'
        if not os.path.exists(gcc):
            raise RuntimeError(f'Failed to install the raspi {arch} toolchain: {gcc} is still missing')
        version = execute_piped([gcc, '-dumpfullversion'], throw=False) or '?'
        console(f'Installed {triple} gcc {version}', color=Color.GREEN)
        console(f'Build with: mama build {"raspi" if arch == "arm64" else "raspi32"}')


    def run_convenient_installs(self):
        for tool in self.convenient_install:
            if tool.startswith('raspi-'): self.install_raspi(tool[6:])
            elif 'clang-' in tool: self.install_clang(tool[6:])
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

    def is_network_available(self) -> bool:
        """Lazily cached: True until a clearly network-related failure marks it False."""
        return self._network_available is not False

    def mark_network_unavailable(self):
        if self._network_available is not False:
            if self.print: warning('  Network unavailable - using cached packages where possible')
            self._network_available = False

