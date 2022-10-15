import os, sys, multiprocessing, tempfile
import mama.util as util
from .utils.system import System, console

if System.linux:
    import distro


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
        self.reclone   = False
        self.mama_init = False
        self.print     = True
        self.verbose   = False
        self.test      = ''
        self.start     = ''
        # supported platforms
        self.windows = False
        self.linux   = False
        self.macos   = False
        self.ios     = False
        self.android = False
        self.raspi   = False
        self.oclea    = False
        # compilers
        self.clang = True # prefer clang on linux
        self.gcc   = False
        self.clang_path = ''
        self.gcc_path = ''
        self.compiler_cmd = False # Was compiler specificed from command line?
        self.fortran = ''
        # build optimization
        self.release = True
        self.debug   = False
        # valid architectures: x86, x64, arm, arm64
        self.arch    = None
        self.jobs    = multiprocessing.cpu_count()
        self.target  = None
        self.flags   = None
        self.open    = None
        self.ios_version   = '11.0'
        self.macos_version = '10.12'
        ## Artifactory URL for dependency uploads and downloads
        self.artifactory_ftp = None
        self.artifactory_auth = None
        ## Ninja
        self.ninja_path = self.find_ninja_build()
        ## MSVC, MSBuild
        self._visualstudio_path = None
        self._visualstudio_cmake_id = None
        self._msbuild_path = None
        self._msvctools_path = None
        ## Android
        self.android_sdk_path = ''
        self.android_ndk_path = ''
        self.android_ndk_release = ''
        self.android_api     = 'android-24'
        self.android_ndk_stl = 'c++_shared' # LLVM libc++
        ## Raspberry PI - Raspi
        self.raspi_compilers  = ''  ## Raspberry g++ and gcc
        self.raspi_system     = ''  ## path to Raspberry system libraries
        self.raspi_include_paths = [] ## path to additional Raspberry include dirs
        ## Oclea CV25/CVXX
        self.oclea_compilers = ''  ## Oclea g++, gcc and ld
        self.oclea_system    = ''  ## Path to Oclea system libraries
        self.oclea_include_paths = []  ## Path to additional Oclea include dirs
        ## Convenient installation utils:
        self.convenient_install = []
        ## Workspace and parsing
        self.global_workspace = False
        if System.windows:
            self.workspaces_root = util.normalized_path(os.getenv('HOMEPATH'))
        else:
            self.workspaces_root = os.getenv('HOME')
        self.unused_args = []
        self.parse_args(args)
        self.check_platform()


    def parse_args(self, args):
        for arg in args:
            if   arg == 'list':      self.list    = True
            elif arg == 'build':     self.build   = True
            elif arg == 'clean':     self.clean   = True
            elif arg == 'rebuild':   self.rebuild = True
            elif arg == 'update':    self.update  = True
            elif arg == 'deploy':    self.deploy  = True
            elif arg == 'upload':    self.upload  = True
            # Updates, Builds and Deploys the project as a package
            elif arg == 'serve':
                self.build = True
                self.update = True
                self.deploy = True
            elif arg == 'reclone':
                console('WARNING: Argument `reclone` is deprecated, use `wipe` instead.')
                self.reclone = True
            elif arg == 'wipe':      self.reclone = True
            elif arg == 'init':      self.mama_init = True
            elif arg == 'silent':    self.print = False
            elif arg == 'verbose':   self.verbose = True
            elif arg == 'all':       self.target = 'all'
            elif arg == 'test':      self.test = ' ' # no test arguments
            elif arg == 'start':     self.start = ' ' # no start arguments
            elif arg == 'windows': self.set_platform(windows=True)
            elif arg == 'linux':   self.set_platform(linux=True)
            elif arg == 'macos':   self.set_platform(macos=True)
            elif arg == 'ios':     self.set_platform(ios=True)
            elif arg == 'android': self.set_platform(android=True)
            elif arg == 'raspi':   self.set_platform(raspi=True)
            elif arg == 'oclea':   self.set_platform(oclea=True)
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
            elif arg == 'fortran=': self.fortran = arg[8:]
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
            # Calls target.start with the specified arguments
            # Ex: mama build start=verify
            elif arg.startswith('start='):  self.start = self.join_args(self.start, arg[6:])
            elif arg.startswith('arch='):   self.set_arch(arg[5:])
            # Add additional compiler flags
            elif arg.startswith('flags='):  self.flags = self.join_args(self.flags, arg[6:])
            # Ex: mama build android-24
            elif arg.startswith('android-'):
                self.set_platform(android=True)
                self.android_api = arg
            elif arg == 'install-clang6':  self.convenient_install.append('clang6')
            elif arg == 'install-clang11': self.convenient_install.append('clang11')
            elif arg == 'install-msbuild': self.convenient_install.append('msbuild')
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


    def set_platform(self, windows=False, linux=False, macos=False, \
                           ios=False, android=False, raspi=False, oclea=False):
        self.windows = windows
        self.linux   = linux
        self.macos   = macos
        self.ios     = ios
        self.android = android
        self.raspi   = raspi
        self.oclea   = oclea
        return True


    def is_platform_set(self):
        return self.windows or self.linux or self.macos \
            or self.ios or self.android or self.raspi or self.oclea


    def check_platform(self):
        if not self.is_platform_set():
            self.set_platform(windows=System.windows, linux=System.linux, macos=System.macos)
            if not self.is_platform_set():
                raise RuntimeError(f'Unsupported platform {sys.platform}: Please specify platform!')

        # set defaults if arch was not specified
        if not self.arch:
            if self.macos:        self.set_arch('x64')
            elif self.ios:        self.set_arch('arm64')
            elif self.android:    self.set_arch('arm64')
            elif self.raspi:      self.set_arch('arm')
            elif self.oclea:      self.set_arch('arm64')
            elif System.is_64bit: self.set_arch('x64')
            else:                 self.set_arch('x86')

        # Arch itself is validated in set_arch(), 
        # however we need to validate if arch is allowed on platform
        if self.arch:
            if self.linux and 'arm' in self.arch:
                raise RuntimeError(f'Unsupported arch={self.arch} on linux platform! Build with android instead')
            if self.raspi and self.arch != 'arm':
                raise RuntimeError(f'Unsupported arch={self.arch} on raspi platform! Supported=arm')
            if self.oclea and self.arch != 'arm64':
                raise RuntimeError(f'Unsupported arch={self.arch} on Oclea platform! Supported=arm64')


    def set_arch(self, arch):
        arches = ['x86', 'x64', 'arm', 'arm64']
        if not arch in arches:
            raise RuntimeError(f"Unrecognized architecture {arch}! Valid options are: {arches}")
        self.arch = arch


    def is_64bit_build(self):
        return (self.arch == 'x64' or self.arch == 'arm64')


    def name(self):
        if self.windows: return 'windows'
        if self.linux:   return 'linux'
        if self.macos:   return 'macos'
        if self.ios:     return 'ios'
        if self.android: return 'android'
        if self.raspi:   return 'raspi'
        if self.oclea:   return 'oclea'
        return 'build'


    def platform_name(self):
        """
        Gets the build folder name depending on platform and architecture.
        By default 64-bit architectures use the platform name, eg 'windows' or 'linux'
        And 32-bit architectures add a suffix, eg 'windows32' or 'linux32'
        """
        # WARNING: This needs to be in sync with dependency_chain.py: _save_mama_cmake !!!
        if self.windows:
            if self.is_target_arch_x64(): return 'windows'
            if self.is_target_arch_x86(): return 'windows32'
            if self.is_target_arch_armv7(): return 'winarm32'
            return 'winarm'
        if self.linux:
            if self.is_target_arch_x64(): return 'linux'
            return 'linux32'
        if self.macos: return 'macos'  # Apple dropped 32-bit support
        if self.ios:   return 'ios'    # Apple dropped 32-bit support
        if self.android:
            if self.is_target_arch_arm64(): return 'android'
            return 'android32'
        if self.raspi: return 'raspi32'  # Only 32-bit raspi
        if self.oclea: return 'oclea64'  # Only 64-bit oclea aarch64 (arm64)
        return 'build'


    def set_build_config(self, release=False, debug=False):
        self.release = release
        self.debug   = debug
        return True


    def set_artifactory_ftp(self, ftp_url, auth='store'):
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
                console(f'Target {target_name} requests Clang. Using Clang since no explicit compiler set.')
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
                console(f'Target {target_name} requests GCC. Using GCC since no explicit compiler set.')
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


    # returns: root path where the compilers exist and the discovered suffix
    def find_compiler_root(self, suggested_path, compiler, suffixes):
        roots = []
        if suggested_path: roots.append(suggested_path)
        roots += ['/etc/alternatives/', '/usr/bin/', '/usr/local/bin/', '/bin/']
        for root in roots:
            for suffix in suffixes:
                if os.path.exists(root + compiler + suffix):
                    return (root, suffix)
        raise EnvironmentError(f'Could not find {compiler} from {roots} with any suffix {suffixes}')


    def get_preferred_compiler_paths(self, cxx_enabled):
        if self.raspi:  # only GCC available for this platform
            ext = '.exe' if System.windows else ''
            cc  = f'{self.raspi_bin()}arm-linux-gnueabihf-gcc{ext}'
            cxx = f'{self.raspi_bin()}arm-linux-gnueabihf-g++{ext}'
            return (cc, cxx)
        if self.oclea:
            cc  = f'{self.oclea_bin()}aarch64-oclea-linux-gcc'
            cxx = f'{self.oclea_bin()}aarch64-oclea-linux-g++'
            return (cc, cxx)
        if self.clang:
            key = 'clang++' if cxx_enabled else 'clang'
            self.clang_path, suffix = self.find_compiler_root(self.clang_path, key, ['-12','-11','-10','-9','-8','-7','-6',''])
            cc = f'{self.clang_path}clang{suffix}'
            cxx = f'{self.clang_path}clang++{suffix}'
            return (cc, cxx)
        if self.gcc:
            key = 'g++' if cxx_enabled else 'gcc'
            self.gcc_path, suffix = self.find_compiler_root(self.gcc_path, key, ['-11','-10','-9','-8','-7','-6',''])
            cc = f'{self.gcc_path}gcc{suffix}'
            cxx = f'{self.gcc_path}g++{suffix}'
            return (cc, cxx)
        raise EnvironmentError('No preferred compiler for this platform!')


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


    def append_env_path(self, paths, env):
        path = os.getenv(env)
        if path: paths.append(path)


    def android_abi(self):
        if self.is_target_arch_armv7(): return 'armeabi-v7a'
        elif self.arch == 'arm64': return 'arm64-v8a'
        else: raise RuntimeError(f'Unrecognized android arch: {self.arch}')


    def android_home(self):
        if not self.android_sdk_path: self.init_ndk_path()
        return self.android_sdk_path


    def android_ndk(self):
        if not self.android_ndk_path: self.init_ndk_path()
        return self.android_ndk_path
    

    def raspi_bin(self):
        if not self.raspi_compilers: self.init_raspi_path()
        return self.raspi_compilers


    def raspi_sysroot(self):
        if not self.raspi_compilers: self.init_raspi_path()
        return self.raspi_system

    
    def raspi_includes(self):
        if not self.raspi_compilers: self.init_raspi_path()
        return self.raspi_include_paths
    

    def oclea_bin(self):
        if not self.oclea_compilers: self.init_oclea_path()
        return self.oclea_compilers


    def oclea_sysroot(self):
        if not self.oclea_compilers: self.init_oclea_path()
        return self.oclea_system

    
    def oclea_includes(self):
        if not self.oclea_compilers: self.init_oclea_path()
        return self.oclea_include_paths


    def init_ndk_path(self):
        paths = []
        self.append_env_path(paths, 'ANDROID_HOME')
        if System.windows: paths += [f'{os.getenv("LOCALAPPDATA")}\\Android\\Sdk']
        elif System.linux: paths += [f'{os.getenv("HOME")}/Android/Sdk', '/usr/bin/android-sdk', '/opt/android-sdk']
        elif System.macos: paths += [f'{os.getenv("HOME")}/Library/Android/sdk']
        ext = '.cmd' if System.windows else ''
        for sdk_path in paths:
            if os.path.exists(f'{sdk_path}/ndk-bundle/ndk-build{ext}'):
                self.android_sdk_path = sdk_path
                self.android_ndk_path = sdk_path  + '/ndk-bundle'
                self.android_ndk_release = 'r16b'
                if self.print: console(f'Found Android NDK: {self.android_ndk_path}')
                return
        raise EnvironmentError(f'''Could not detect any Android NDK installations. 
Default search paths: {paths} 
Define env ANDROID_HOME with path to Android SDK with NDK at ${{ANDROID_HOME}}/ndk-bundle.''')


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


    def init_oclea_path(self):
        if not System.linux: raise RuntimeError('Oclea only supported on Linux')
        paths = []
        self.append_env_path(paths, 'OCLEA_HOME')
        self.append_env_path(paths, 'OCLEA_SDK')
        if System.linux: paths += ['/usr/bin/oclea', '/usr/local/bin/oclea']
        compiler = ''
        if System.linux: compiler = 'x86_64-ocleasdk-linux/usr/bin/aarch64-oclea-linux/aarch64-oclea-linux-gcc'
        for oclea_path in paths:
            if os.path.exists(f'{oclea_path}/{compiler}'):
                self.oclea_compilers = f'{oclea_path}/x86_64-ocleasdk-linux/usr/bin/aarch64-oclea-linux/'
                self.oclea_system    = f'{oclea_path}/x86_64-ocleasdk-linux/usr/lib'
                self.oclea_include_paths = [f'{oclea_path}/aarch64-oclea-linux/usr/include']
                if self.print: console(f'Found Oclea TOOLS: {self.oclea_compilers}\n    sysroot: {self.oclea_system}')
                return
        raise EnvironmentError(f'''No Oclea toolchain compilers detected! 
Default search paths: {paths} 
Define env OCLEA_HOME with path to Oclea tools.''')


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
        vspath = util.execute_piped(f'{vswhere_exe} -latest -nologo -property installationPath')
        if vspath and os.path.exists(vspath):
            self._visualstudio_path = vspath
            if self.verbose: console(f'Detected VisualStudio: {vspath}')
            return vspath
        
        paths = []
        vs_variants = [ 'Enterprise', 'Professional', 'Community'  ]
        for version in [ '2022' ]: # new 64-bit VS
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
        if self.is_target_arch_x64():
            return 'native' if System.is_64bit else 'x86-64'
        if self.is_target_arch_x86():
            return 'pentium4' if System.is_64bit else 'native'
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
            paths.append(f"{util.execute_piped(vswhere)}\\MSBuild\\Current\\Bin\\MSBuild.exe")
            paths.append(f"{util.execute_piped(vswhere)}\\MSBuild\\15.0\\Bin\\amd64\\MSBuild.exe")
            
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


    def install_clang6(self):
        if System.windows: raise OSError('Install Visual Studio 2019 with Clang support')
        if System.macos:   raise OSError('Install Xcode to get Clang on macOS')
        
        suffix = '1404'
        try:
            dist = distro.info()
            if dist['id'] != "ubuntu": raise OSError('install_clang6 only supports Ubuntu')
            majorVersion = int(dist['version_parts']['major'])
            if majorVersion >= 16:
                suffix = '1604'
            console(f'Choosing {suffix} for kernel major={majorVersion}')
        except Exception as err:
            console(f'Failed to parse linux distro; falling back to {suffix}: {err}')

        self.install_clang(clang_major='6', clang_ver='6.0', suffix=suffix)


    def install_clang11(self):
        if System.windows: raise OSError('Install Visual Studio 2019 with Clang support')
        if System.macos:   raise OSError('Install Xcode to get Clang on macOS')
        
        suffix = '1604'
        try:
            dist = distro.info()
            if dist['id'] != "ubuntu": raise OSError('install_clang11 only supports Ubuntu')
            majorVersion = int(dist['version_parts']['major'])
            minorVersion = int(dist['version_parts']['minor'])
            if majorVersion >= 20 and minorVersion >= 10:
                suffix = '2010'
            elif majorVersion >= 20:
                suffix = '2004'
            elif majorVersion >= 16:
                suffix = '1604'
            console(f'Choosing {suffix} for kernel major={majorVersion} minor={minorVersion}')
        except Exception as err:
            console(f'Failed to parse linux distro; falling back to {suffix}: {err}')

        self.install_clang(clang_major='11', clang_ver='11.0', suffix=suffix)


    def install_clang(self, clang_major, clang_ver, suffix):
        clang_major = '11'
        clang_ver = '11.0'
        clangpp = f'clang++{clang_major}'
        clang_zip = util.download_file(f'http://ateh10.net/dev/{clangpp}-{suffix}.zip', tempfile.gettempdir())
        console(f'Installing to /usr/local/{clangpp}')
        util.execute(f'sudo rm -rf /usr/local/{clangpp}') # get rid of any old stuff
        util.execute(f'cd /usr/local && sudo unzip -oq {clang_zip}') # extract /usr/local/clang++11/
        os.remove(clang_zip)
        util.execute(f'sudo ln -sf /usr/local/{clangpp}/lib/libc++.so.1    /usr/lib')
        util.execute(f'sudo ln -sf /usr/local/{clangpp}/lib/libc++abi.so.1 /usr/lib')
        util.execute(f'sudo ln -sf /usr/local/{clangpp}/bin/clang      /usr/bin/clang-{clang_ver}')
        util.execute(f'sudo ln -sf /usr/local/{clangpp}/bin/clang++    /usr/bin/clang++-{clang_ver}')
        util.execute(f'sudo ln -sf /usr/local/{clangpp}/include/c++/v1 /usr/include/c++/v1')
        util.execute(f'sudo update-alternatives --install /usr/bin/clang   clang   /usr/bin/clang-{clang_ver}   100')
        util.execute(f'sudo update-alternatives --install /usr/bin/clang++ clang++ /usr/bin/clang++-{clang_ver} 100')
        util.execute(f'sudo update-alternatives --set clang   /usr/bin/clang-{clang_ver}')
        util.execute(f'sudo update-alternatives --set clang++ /usr/bin/clang++-{clang_ver}')


    def install_msbuild(self):
        if System.windows: raise OSError('Install Visual Studio 2019 to get MSBuild on Windows')
        if System.macos:   raise OSError('install_msbuild not implemented for macOS')
        
        dist = distro.info()
        if dist['id'] != "ubuntu": raise OSError('install_msbuild only supports Ubuntu')
        codename = dist['codename']

        util.execute('curl https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor > /tmp/microsoft.gpg')
        util.execute('sudo mv /tmp/microsoft.gpg /etc/apt/trusted.gpg.d/microsoft.gpg')
        util.execute(f"sudo sh -c 'echo \"deb [arch=amd64] https://packages.microsoft.com/repos/microsoft-ubuntu-{codename}-prod {codename} main\" > /etc/apt/sources.list.d/dotnetdev.list'")
        util.execute('sudo apt-get install apt-transport-https')
        util.execute('sudo apt-get update')
        util.execute('sudo apt-get install dotnet-sdk-2.1')


    def run_convenient_installs(self):
        if 'clang6'  in self.convenient_install: self.install_clang6()
        if 'clang11' in self.convenient_install: self.install_clang11()
        if 'msbuild' in self.convenient_install: self.install_msbuild()


    def libname(self, library):
        if self.windows: return f'{library}.lib'
        else:            return f'lib{library}.a'


    def libext(self):
        return 'lib' if self.windows else 'a'


    def target_matches(self, target_name):
        return self.target == 'all' or self.target == target_name


    def no_specific_target(self):
        return (not self.target) or (self.target == 'all')

