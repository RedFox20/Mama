from __future__ import annotations
import os, sys, tempfile, shutil, threading, time, contextlib  # psutil is deferred, see _default_build_jobs
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
from mama.platforms.platform import ARCHES, Platform
from mama.platforms.registry import platform_for_arg
from .utils import git_status
from .utils.archive import unzip
from .utils.errors import BuildError
from .utils.fileio import find_executable_from_system
from .utils.net import REQUIRED_DOWNLOAD_TIMEOUT, download_file
from .utils.paths import forward_slashes, normalized_path
from .utils.system import System, console, Color, error, warning
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
        from .utils.progress import get_time_str
        return f'Updated {self.total} target(s): {", ".join(parts)} in {get_time_str(self._duration)}'


class DeployStats:
    """What the deploy of the target this run named wrote, for the one-line summary of the build.

    Scoped, not global: a tree of 30 deps deploys far more than the user asked about. The window opens
    around the deploy hook of the current target, so a package that hook delegates to counts too."""
    def __init__(self):
        self._lock = threading.Lock()
        self._scope = threading.local()  # only the thread inside the current target's deploy records
        self.counts = [0, 0, 0, 0]  # includes, libs, syslibs, assets
        self.dirs = set()

    @contextlib.contextmanager
    def recording(self, enabled=True):
        """Count every papa deploy this thread makes until the block ends. Re-entrant, because a build
        hook that deploys sits inside its own window. `enabled=False` changes nothing, so a nested
        deploy of another target cannot close the window of its caller."""
        if not enabled:
            yield
            return
        self._scope.depth = getattr(self._scope, 'depth', 0) + 1
        try: yield
        finally: self._scope.depth -= 1

    def record(self, out_dir:str, counts):
        if getattr(self._scope, 'depth', 0) <= 0: return
        with self._lock:
            self.counts = [total + n for total, n in zip(self.counts, counts)]
            self.dirs.add(out_dir)

    def summary_line(self) -> str:
        """One-line summary, or '' when this run deployed nothing."""
        if not self.dirs: return ''
        includes, libs, syslibs, assets = self.counts
        what = f'{includes} includes, {libs} libs'
        if syslibs: what += f', {syslibs} syslibs'
        if assets:  what += f', {assets} assets'
        where = next(iter(self.dirs)) if len(self.dirs) == 1 else f'{len(self.dirs)} package dirs'
        return f'Deployed {what} to {where}'


class BuildConfig:
    """The one build configuration, created in the root project working directory.
    Every dependency shares it."""
    @staticmethod
    def _default_build_jobs() -> int:
        """Default parallel build jobs: Linux keeps ONE core free, so a full build cannot freeze the
        desktop or trip the OOM killer. Windows and macOS use all cores. `jobs=N` overrides this."""
        import psutil  # deferred: psutil costs about 32ms to import, and only this line needs it
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
        # upload the deploy archive over SFTP, if the root mamafile defines an artifactory URL
        self.upload  = False
        # for uploads only: upload only when the package is not on the server yet
        self.if_needed = False
        # `unpublish=<selector>`: '' when the run does not unpublish. See artifactory_unpublish.select
        self.unpublish = ''
        self.unpublish_keep = None  # how many versions `prune-old` leaves. None takes the module default,
                                    # so an explicit `prune-old=0` can still mean keep nothing
        self.assume_yes = False  # `yes` on the command line answers the unpublish prompt
        # The target the user typed. `update` rewrites self.target to `all` at main.py, and an unpublish
        # that read that would delete every version of every dep in the graph.
        self.user_target = None
        # `art`: artifactory download is mandatory, no source builds
        self.force_artifactory = False
        # `noart`: ignore artifactory for this run
        self.disable_artifactory = False
        self.reclone   = False
        self.dirty     = False # mark a target for rebuild on the next build even if it is up to date
        self.deps_only = False # only execute build/rebuild/clean on dependencies, not the main target
        self.sched_debug = False # TEMP: print each target's build-weight calc, then exit without building
        self.buildstats = False # after the build, print a per-package load/configure/build time breakdown
        self.unshallow = False  # git clones are shallow by default, this flag unshallows them
        self.git_url_override = None  # 'https' or 'ssh': rewrite add_git() urls at build time
        self.run_cmake_configure = False # force the CMake configure step even when the target needs no rebuild
        self.mama_init = False
        self.print     = True
        self.verbose   = False
        self.test      = ''
        self.start     = ''
        self.with_tests = False # forces -DENABLE_TESTS=ON
        self.test_until_failure = 0 # if > 0, run the test executable in a loop until it fails, to catch a flaky test
        self.sanitize  = None # gcc/clang: -fsanitize=[thread|leak|address|undefined]
        self.coverage  = None # gcc/clang: gcov | msvc: /fsanitize-coverage=edge
        self.coverage_report = None # runs gcovr to generate coverage report
        self.update_stats = UpdateStats() # clone/pull/shim counters for the load phase summary
        self.deploy_stats = DeployStats() # what the papa deploys of this run wrote
        self.enable_clang_tidy = False # enables clang-tidy static analysis during build
        self.clang_tidy_path = None # resolved path to clang-tidy executable
        # the ONE active platform: set_platform() derives the mamafile flags below from it, and nothing else stores platform state
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
        # override the C and C++ compiler paths
        self.cc_path = ''
        self.cxx_path = ''
        self.cxx_version = '' # c++ compiler version, eg '8.3.0' for gcc 8.3.0
        # True when the command line named a compiler, eg `mama build gcc` or `mama build clang`
        self.compiler_cmd = False
        # `clang` or `gcc` on the command line, which `compiler_cmd` cannot answer: the root settings()
        # lock sets that one for every run. A host build child inherits this choice and no other.
        self.compiler_from_args = False
        self.compiler_conflict_warned = False  # the "target prefers X but compiler locked to Y" note fires once, not per dep
        self.clang_stdlib = 'libc++'  # linux clang C++ stdlib, see use_gcc_stdlib_for_clang()
        self.fortran = ''
        # build optimization
        self.release = True
        self.debug   = False
        # target architecture, see set_arch() for the valid names
        self.arch    = None
        self.target_march = {}  # arch to -march, see set_target_march(). Empty means the platform default
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
        self._ninja_version = None # measured once, see ninja_version()
        self.prefer_ninja = not System.windows # do not prefer ninja on Windows by default
        ## Convenient installation utils:
        self.convenient_install = []
        ## Workspace and parsing
        self.parallel_load = False  ## Load dependencies in parallel
        self.serial_load   = False  ## If True, override the auto-parallel-on-update behavior
        self.parallel_max  = 20     ## Cap concurrent git fetches, so the SSH master does not overload
        # The toolchain file a platform picked, '' for a native build. cmake_configure records it and
        # reads it back: a toolchain file owns compiler selection, so mama must not name a compiler too.
        self.cmake_toolchain_file = ''
        self.git_timeout   = 30     ## Kill a git clone/fetch with no progress for this many seconds
        self.no_compiler_cache = False  ## Disable cross-build-dir reuse of cmake compiler detection
        # Where the compiler seed lives. False keeps it in the workspace, so `rm -rf packages/` heals a
        # broken seed. True moves it to the user cache dir, where one probe serves every checkout on this
        # machine. MAMA_GLOBAL_COMPILER_CACHE=1 turns it on for a whole test session or CI job.
        self.global_compiler_cache = os.getenv('MAMA_GLOBAL_COMPILER_CACHE') == '1'
        self.global_workspace = False
        # The root project dir, set by mamabuild from source_dir. A `mama <host> build` bootstrap child
        # uses it as cwd, so it resolves the same dependency graph. None until mamabuild runs (direct-construct tests).
        self.root_source_dir = None
        if System.windows:
            self.workspaces_root = normalized_path(os.getenv('HOMEPATH'))
        else:
            self.workspaces_root = os.getenv('HOME')
        self._network_available = None  # None=untested, True/False=result
        self._announced = set()          # announce_once() keys already printed
        self._announce_lock = threading.Lock()
        self._cmake_ver_num = {}     # cmake command -> version, also the CMakeFiles/<ver> dir name
        self._seed_coord = None      # compiler-seed Coordinator, built on first configure
        self._buildstats_start = None  # buildstats wall start, set only on a non-MSVC insights run
        self._timetrace_json = None    # vcperf trace path, set only on an MSVC insights run
        self.unused_args = []
        self.loaded_dependencies : dict[str, BuildDependency] = {}
        self.dep_registry_lock = threading.Lock()  # guards loaded_dependencies under parallel_load
        self.parse_args(args)
        # `deps_only` means act on the dependencies and not the target, and `in_scope` names the target.
        # Rather than guess which one wins over a delete, refuse the pair.
        if self.deps_only and self.unpublish:
            raise RuntimeError('deps_only cannot combine with unpublish. Name the dependency to ' + \
                               'unpublish instead, as `mama <dep> unpublish=<selector>`.')
        self.remember_user_target()  # before any command rewrites self.target
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
            elif arg == 'yes' or arg == 'y': self.assume_yes = True
            # `unpublish=current|<version>|prune-old[=N]|prune-all` deletes published archives
            elif arg.startswith('unpublish='): self.set_unpublish(arg[10:])
            elif arg == 'unpublish':
                raise RuntimeError('unpublish needs a selector: unpublish=current, unpublish=<version>, ' + \
                                   'unpublish=prune-old[=N] or unpublish=prune-all')
            elif arg == 'if_needed': self.if_needed = True
            elif arg == 'art':       self.force_artifactory = True
            elif arg == 'noart':     self.disable_artifactory = True
            # update, rebuild, deploy and upload the project as a package
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
            # every working-tree check then names the dep, the caller and what it found, see _log_status_check
            elif arg == 'verbose':   self.verbose = True; git_status.log_status_checks = True
            elif arg == 'parallel':  self.parallel_load = True
            elif arg == 'serial':    self.serial_load = True
            elif arg == 'nocache' or arg == 'no-compiler-cache': self.no_compiler_cache = True
            elif arg == 'globalcache': self.global_compiler_cache = True  # seed in the user cache, not in packages/
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
                self.compiler_cmd = self.compiler_from_args = True
            elif arg == 'gcc':
                self.gcc = True
                self.clang = False
                self.compiler_cmd = self.compiler_from_args = True
            elif arg == 'fortran': self.fortran = self.find_default_fortran_compiler()
            elif arg.startswith('fortran='): self.fortran = arg[8:]
            elif arg == 'release': self.set_build_config(release=True)
            elif arg == 'debug':   self.set_build_config(debug=True)
            elif arg == 'open':    self.open = 'root'
            # open a target source dir in the editor: `mama open ReCpp`, or the old form `open=ReCpp`
            elif arg.startswith('open='):   self.open = arg[5:]
            elif arg.startswith('jobs='):   self.jobs = int(arg[5:])
            # old target form `target=opencv`: the automatic lookup (`mama build opencv`) supersedes it
            elif arg.startswith('target='): self.target = arg[7:]
            # test runner args: test="a b", repeated test=a test=b, or the comma form test=a,b
            elif arg.startswith('test='):   self.test = self.join_args(self.test, arg[5:])
            # run the tests in a loop until failure, to catch a flaky test
            elif arg == 'test_until_failure': self.test_until_failure = 100 # arbitrary default
            elif arg.startswith('test_until_failure='): self.test_until_failure = int(arg[19:]) # max loop iterations
            # call target.start with the given arguments, eg start=verify
            elif arg.startswith('start='):  self.start = self.join_args(self.start, arg[6:])
            elif arg.startswith('arch='):   self.set_arch(arg[5:])
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


    # parse `arg` and append its contents to `args`
    def join_args(self, args, arg):
        if arg[0] == '"' and arg[-1] == '"':
            arg = arg[1:-1]
        elif ',' in arg:
            arg = ' '.join(arg.split(','))
        if not args:
            return arg
        return args + ' ' + arg


    ## set_platform() flag to platform class name, in resolution order: the FIRST enabled flag wins.
    ## The lookup goes through this module's globals, so a test can monkeypatch a platform class.
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
        """Install `cls` as the one active platform. The SAME class twice keeps the existing
        instance, so `mama build android-31 ndk-28` does not discard the api level."""
        if cls is None:                     self.platform = None
        elif type(self.platform) is not cls: self.platform = cls(self)
        self._update_platform_flags()


    def _update_platform_flags(self):
        """Refresh the mamafile-facing platform properties. `msvc`, `linux` and `macos` stay bools. The
        rest return the platform object, because a mamafile reads fields like `config.android.android_api`."""
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
        # convenience alias that matches any embedded Yocto Linux platform (Oclea, Xilinx, IMX8MP)
        self.yocto_linux = obj(GenericYocto)


    def is_platform_set(self):
        return self.platform is not None


    def check_platform(self):
        if not self.is_platform_set():
            # choose MSVC on Windows, unless the user named gcc or clang on the command line
            msvc = System.windows and not (self.gcc or self.clang)
            self.set_platform(msvc=msvc, linux=System.linux, macos=System.macos)
            if not self.is_platform_set():
                raise RuntimeError(f'Unsupported platform {sys.platform}: Please specify platform!')
        if not self.msvc and not (self.gcc or self.clang):
            self.gcc = True # default to GCC on non-MSVC platforms

        if not self.arch: self.set_arch(self.platform.get_default_arch())
        self.platform.validate_arch(self.arch) # set_arch() only checks the arch name itself

        if self.enable_clang_tidy:
            self.set_clang_tidy_path(self.clang_tidy_path)


    def get_distro_info(self):
        if not self.distro:
            self.distro = self.platform.distro_version()
        return self.distro


    def remember_user_target(self):
        """Freeze the target the user named, before any command rewrites it."""
        self.user_target = self.target


    def set_unpublish(self, selector: str):
        """Read `unpublish=<selector>`. `prune-old=N` carries its own count, and `current` becomes ''
        here, because the version this checkout resolves to is only known once the target loads."""
        selector, _, count = selector.partition('=')
        if selector == 'prune-old' and count:
            if not count.isdigit(): raise RuntimeError(f'unpublish=prune-old={count} needs a whole number')
            self.unpublish_keep = int(count)
        elif count:
            raise RuntimeError(f'unpublish={selector} takes no `={count}`, only prune-old does')
        if not selector:
            raise RuntimeError('unpublish= needs current, a version, prune-old[=N] or prune-all')
        self.unpublish = selector


    def set_arch(self, arch):
        if not arch in ARCHES:
            raise RuntimeError(f"Unrecognized architecture {arch}! Valid options are: {list(ARCHES)}")
        self.arch = arch


    def set_target_march(self, arch: str, march: str):
        """Pin the compiler -march for one target arch, for the root target and every dependency.
        Call it from the root mamafile settings(), before any dependency loads.

        The platform default for a native build is `-march=native`, which bakes the CPU of the build
        machine into the binary. A release that has to run on other machines pins a baseline instead.

        The pin names the build. A pinned build gets its own build dir and its own artifactory archive.
        Its objects can never link into a build tuned for another instruction set.

        A platform whose compiler has no -march warns and keeps the default, so target_march only ever
        holds a pin that reaches the compiler.
        arch: a target arch name, see set_arch()
        march: the -march value, for example 'x86-64-v3'. An empty value drops the pin
        """
        if arch not in ARCHES:
            raise RuntimeError(f'set_target_march: unknown arch {arch}! Valid options are: {list(ARCHES)}')
        march = march.strip()
        if '=' in march or ' ' in march:
            raise RuntimeError(f'set_target_march: {march} must be the value alone, as in x86-64-v3.')
        if not march:
            self.target_march.pop(arch, None)
        elif self.platform and not self.platform.supports_march:
            warning(f'{self.platform.name} has no -march. Ignoring set_target_march({arch}, {march}).')
        else:
            self.target_march[arch] = march


    def is_64bit_build(self):
        return (self.arch == 'x64' or self.arch == 'arm64')


    def name(self):
        return self.platform.name if self.platform else 'build'


    def host_platform_name(self):
        """Build-dir / CLI name of the HOST mama runs on ('windows'|'linux'|'macos'), independent of the
        cross-compile target. The single source of truth for host build dirs and a `mama <host> build`
        bootstrap. The host arch stays out, because the `windows` and `linux` build-dir names mean x64."""
        if System.windows: return 'windows'
        if System.macos:   return 'macos'
        return 'linux'


    def set_build_config(self, release=False, debug=False):
        self.release = release
        self.debug   = debug
        return True


    def set_artifactory_ftp(self, ftp_url: str, auth='store'):
        """ @see BuildTarget.set_artifactory_ftp() for documentation """
        self.artifactory_ftp = ftp_url
        self.artifactory_auth = auth


    def announce_once(self, key: str, text: str):
        """Print `text` only on the first call with `key`. Option builders run per fingerprint
        computation, not per configure, so a plain console() repeats the same line."""
        if not self.print: return
        with self._announce_lock:
            if key in self._announced: return
            self._announced.add(key)
        console(text)


    def clean_only(self) -> bool:
        """`clean` with no build: nothing to fetch, configure or package afterwards - just delete."""
        return self.clean and not self.build


    def lock_compiler(self):
        """Freeze the compiler after the ROOT mamafile's settings(). build_dir depends on it, so a dep that
        flips it mid-load would scatter the tree across linux/ and linux-clang/. A later prefer_*() only prints a note."""
        self.compiler_cmd = True


    def _warn_compiler_conflict(self, target_name, requested, locked):
        """Print the 'target requested X but compiler already set to Y' note ONCE per run. Every dep
        re-requests its preference, so a per-call print repeats the same line for each target."""
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


    def enable_fortran(self, path=''):
        """Enable the Fortran compiler.
        path: custom path or command for the Fortran compiler, default '' searches the system"""
        if self.fortran: return
        self.fortran = path if path else self.find_default_fortran_compiler()


    def find_compiler_root(self, suggested_path, compiler, suffixes, dumpfullversion) -> tuple[str, str, str]:
        """Find the root path that holds the compiler and the discovered name suffix.
        Returns (root_path, suffix, version)."""
        def resolve_compiler(cxx_path, suffix) -> tuple[str, str, str]:
            original_path = cxx_path
            cxx_path = os.path.realpath(cxx_path) # resolve symlinks
            if not os.path.exists(cxx_path):
                return '', '', ''
            version = self.get_gcc_clang_fullversion(cxx_path, dumpfullversion)
            root = forward_slashes(os.path.dirname(cxx_path)) + '/'
            # A symlinked compiler and its target have different names, and the caller composes
            # `root + compiler + suffix`, so try both names, then the dir of the link itself.
            name = os.path.basename(cxx_path)
            real = name[len(compiler):] if name.startswith(compiler) else ''
            spelling = next((s for s in (real, suffix, '') if os.path.exists(f'{root}{compiler}{s}')), None)
            if spelling is None:  # a target-prefixed name composes nothing here, and the link does
                root = forward_slashes(os.path.dirname(original_path)) + '/'
                spelling = next((s for s in (suffix, '') if os.path.exists(f'{root}{compiler}{s}')), suffix)
            return root, spelling, version

        # priority paths first: /etc/alternatives is the user's configured default, ~/.local/bin a manual install
        priority_choices = [ suggested_path, os.getenv('CXX'),
                            f'{os.getenv("HOME")}/.local/bin/{compiler}',
                            '/etc/alternatives/' + compiler ]
        for priority_cxx in priority_choices:
            if priority_cxx and os.path.exists(priority_cxx):
                path, real_suffix, ver = resolve_compiler(priority_cxx, '')
                if ver:
                    if self.verbose:
                        console(f'Compiler {compiler} ({ver}) at {os.path.realpath(priority_cxx)} via {priority_cxx}')
                    return path, real_suffix, ver

        # search every candidate directory for a suitable compiler
        roots = []
        if suggested_path: roots.append(suggested_path)
        roots += ['/etc/alternatives/', '/usr/bin/', '/usr/local/bin/', '/bin/']

        # also search PATH, not only the hardcoded roots. Windows separates its entries with `;`
        path_dirs = (forward_slashes(p) for p in os.getenv('PATH', '').split(os.pathsep) if p)
        roots += [p if p.endswith('/') else p + '/' for p in path_dirs]

        candidates = []
        already_added = set()
        for root in roots:
            for suffix in suffixes:
                cxx_path = root + compiler + suffix # compiler=clang++
                if os.path.exists(cxx_path):
                    path, real_suffix, ver = resolve_compiler(cxx_path, suffix)
                    if ver and not path in already_added:
                        already_added.add(path)
                        candidates.append((path, real_suffix, ver))
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

        # with verbose, print every candidate to debug a CI machine
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

        # a cross platform names its own compilers: a host-compiler fallback would silently build for the wrong arch
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


    def ninja_version(self) -> str:
        """What `ninja --version` answers, measured once. The generated cmake reads this number
        instead of spawning ninja on every configure."""
        if self._ninja_version is None:
            out = execute_piped([self.ninja_path, '--version'], throw=False) if self.ninja_path else ''
            self._ninja_version = (out or '').strip()
        return self._ninja_version


    def get_gcc_clang_fullversion(self, cc_path, dumpfullversion):
        if dumpfullversion:
            version = execute_piped([cc_path, '-dumpfullversion']).strip() # eg 9.4.0
            if version.count('.') >= 1:
                return version
        # recent clang++ releases do not support -dumpfullversion
        return execute_piped([cc_path, '-dumpversion']).strip()


    def compiler_version(self):
        return self.platform.compiler_version_tag()


    def find_ninja_build(self):
        ninja_executables = [
            os.getenv('NINJA'),
            find_executable_from_system('ninja'),
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

        # when the android platform is set, check the NDK toolchain bin dir for clang-tidy
        if self.android:
            ndk_bin = self.android.bin()
            clang_tidy_exe = f'{ndk_bin}/clang-tidy.exe' if System.windows else f'{ndk_bin}/clang-tidy'
            if os.path.exists(clang_tidy_exe):
                self.clang_tidy_path = clang_tidy_exe
                if self.print: console(f'Found clang-tidy in Android NDK bin dir: {clang_tidy_exe}', color=Color.GREEN)
                return

        # resolve symlinks to show the full clang-tidy path (/etc/alternatives/clang-tidy -> /usr/bin/clang-tidy-18)
        clang_tidy_exe = find_executable_from_system('clang-tidy', follow_symlinks=True)
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



    def add_coverage_option(self, option='default'):
        if self.coverage: self.coverage += ',' + option
        else:             self.coverage = option


    def append_env_path(self, paths, env):
        path = os.getenv(env)
        if path: paths.append(path)


    def set_toolchain(self, toolchain_dir=None, toolchain_file=None):
        """Point the active platform at an explicit toolchain, from the ROOT mamafile settings(), eg
        self.config.set_toolchain('/opt/my-imx8mp-sdk'). A platform uses whichever argument it needs.
        toolchain_dir: the SDK root that holds the cross compilers and the sysroot
        toolchain_file: the CMake toolchain file the SDK ships
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
        """Resolve the cross-compile toolchain from the default search paths. MUST run after the ROOT
        mamafile's settings(), so an explicit set_toolchain() there wins. A no-op once a toolchain is set."""
        self.platform.init_default()


    def find_default_fortran_compiler(self):
        paths = []
        if System.linux:
            paths += [find_executable_from_system('gfortran')]

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
        if type(clang_major) != int: clang_major = int(clang_major)
        if System.windows: raise OSError('Install Visual Studio 2026 with Clang support')
        if System.macos:   raise OSError('Install Xcode to get Clang on macOS')
        id, major, minor = self.get_distro_info()
        if id != "ubuntu": raise OSError(f'install-clang-{clang_major} only supports ubuntu')
        console(f'Installing clang-{clang_major} and libc++-{clang_major}-dev from apt repositories', color=Color.MAGENTA)
        execute('sudo apt-get update')
        execute(f'sudo apt-get install clang-{clang_major} clang-tidy-{clang_major} '+\
                f'libc++-{clang_major}-dev libc++abi-{clang_major}-dev -y')
        # make this clang the default via update-alternatives, so mama and the cmake tools find it
        console(f'Configuring clang-{clang_major} as default clang via update-alternatives', color=Color.MAGENTA)
        execute(f'sudo update-alternatives --install /usr/bin/clang   clang   /usr/bin/clang-{clang_major}   100')
        execute(f'sudo update-alternatives --install /usr/bin/clang++ clang++ /usr/bin/clang++-{clang_major} 100')
        execute(f'sudo update-alternatives --install /usr/bin/clang-tidy clang-tidy /usr/bin/clang-tidy-{clang_major} 100')
        execute(f'sudo update-alternatives --install /usr/bin/run-clang-tidy run-clang-tidy' + \
                f' /usr/lib/llvm-{clang_major}/bin/run-clang-tidy 100')
        execute(f'sudo update-alternatives --set clang   /usr/bin/clang-{clang_major}')
        execute(f'sudo update-alternatives --set clang++ /usr/bin/clang++-{clang_major}')
        execute(f'sudo update-alternatives --set clang-tidy /usr/bin/clang-tidy-{clang_major}')
        execute(f'sudo update-alternatives --set run-clang-tidy /usr/lib/llvm-{clang_major}/bin/run-clang-tidy')


    def install_gcc(self, gcc_major):
        if type(gcc_major) != int: gcc_major = int(gcc_major)
        if System.windows: raise OSError('Install MinGW to get GCC on Windows')
        if System.macos:   raise OSError('install-gcc not implemented for macOS')
        id, major, minor = self.get_distro_info()
        if id != "ubuntu": raise OSError(f'install-gcc-{gcc_major} only supports ubuntu')
        console(f'Installing gcc-{gcc_major} and g++-{gcc_major} from apt repositories', color=Color.MAGENTA)
        # a blocked third-party PPA fails the whole update, and the Ubuntu archive still updated
        if execute('sudo apt-get update', throw=False) != 0:
            warning('  apt-get update reported an error, continuing with the lists it fetched')
        execute(f'sudo apt-get install gcc-{gcc_major} g++-{gcc_major} -y')
        # make this gcc the default via update-alternatives, so mama and the cmake tools find it
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
        execute(f"sudo sh -c 'echo \"deb [arch=amd64] https://packages.microsoft.com/repos/" + \
                f"microsoft-ubuntu-{codename}-prod {codename} main\" > /etc/apt/sources.list.d/dotnetdev.list'")
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
        ndk_zip = download_file(ndk_url, tempfile.gettempdir(), timeout=REQUIRED_DOWNLOAD_TIMEOUT)

        if System.windows or System.macos:
            os.makedirs(ndk_dest, exist_ok=True)
        else:
            execute(f'sudo mkdir -p {ndk_dest} && sudo chown -R $USER {ndk_dest}')

        console(f'Extracting NDK to {ndk_dest}/{ndk_version}')
        unzip(ndk_zip, ndk_dest)

        final_dest = f'{ndk_dest}/{ndk_version}'
        if os.path.exists(final_dest):
            shutil.rmtree(final_dest)

        shutil.move(f'{ndk_dest}/android-ndk-{ndk_key}', final_dest)
        if os.path.exists(f'{final_dest}/build'):
            console(f'NDK installed successfully to {final_dest}')
        else:
            raise RuntimeError(f'Failed to install NDK to {final_dest}')

        console(f'Added ANDROID_NDK_HOME={final_dest} to ~/.bashrc. Run source ~/.bashrc or restart the terminal.')
        # remove existing ANDROID_NDK_HOME from bashrc if exists
        execute('sed -i "/export ANDROID_NDK_HOME/d" ~/.bashrc')
        # add new ANDROID_NDK_HOME to bashrc
        execute(f'echo "export ANDROID_NDK_HOME={final_dest}" >> ~/.bashrc')


    def install_raspi(self, arch: str):
        """Install the Raspberry Pi cross toolchain from apt. The packages install to /usr/bin/<triple>-gcc,
        which Raspi.init_default() already searches, so a build works immediately with no env var."""
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
        try:
            for tool in self.convenient_install:
                if tool.startswith('raspi-'): self.install_raspi(tool[6:])
                elif 'clang-' in tool: self.install_clang(tool[6:])
                elif 'gcc-' in tool: self.install_gcc(tool[4:])
                elif 'msbuild' in tool: self.install_msbuild()
                elif 'ndk-'    in tool: self.install_ndk(tool[4:])
        except BuildError as e:  # a failed download reports the reason, and a traceback buries it
            error(str(e))
            exit(-1)


    def libname(self, library):
        if self.msvc: return f'{library}.lib'
        else:         return f'lib{library}.a'


    def libext(self):
        return 'lib' if self.msvc else 'a'


    def has_target(self) -> bool:
        """ True when the cmdline named a target, eg 'all' or 'mypackage' """
        return self.target is not None and len(self.target) > 0

    def no_target(self) -> bool:
        """ True when the cmdline named no target """
        return self.target is None or len(self.target) == 0

    def targets_all(self) -> bool:
        """ True when the cmdline target is 'all' """
        return self.target == 'all'


    def target_matches(self, target_name: str) -> bool:
        """ True when `target_name` matches the cmdline target """
        return self.targets_all() \
            or (self.target and self.target.lower() == target_name.lower())


    def no_specific_target(self) -> bool:
        """ True when the cmdline named no target, or named 'all' """
        return self.no_target() or self.targets_all()

    def is_network_available(self) -> bool:
        """Lazily cached: True until a clearly network-related failure marks it False."""
        return self._network_available is not False

    def mark_network_unavailable(self):
        if self._network_available is not False:
            if self.print: warning('  Network unavailable - using cached packages where possible')
            self._network_available = False

