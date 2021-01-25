import os, sys, multiprocessing, subprocess, tempfile
from mama.system import System, console, execute, execute_piped
from mama.util import download_file, unzip, forward_slashes

if System.linux:
    import distro


def find_executable_from_system(name):
    finder = 'where' if System.windows else 'which'
    output = subprocess.run([finder, name], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL).stdout.decode('utf-8')
    output = output.split('\n')[0].strip()
    return output if os.path.isfile(output) else ''


###
# Mama Build Configuration is created only once in the root project working directory
# This configuration is then passed down to dependencies
#
class BuildConfig:
    def __init__(self, args):
        self.build   = False
        self.clean   = False
        self.rebuild = False
        self.update  = False
        self.deploy  = False
        self.reclone   = False
        self.mama_init = False
        self.print     = True
        self.verbose   = False
        self.test      = ''
        self.start     = ''
        self.windows = False
        self.linux   = False
        self.macos   = False
        self.ios     = False
        self.android = False
        self.raspi   = False
        self.clang = True # prefer clang on linux
        self.gcc   = False
        self.clang_path = ''
        self.gcc_path = ''
        self.compiler_cmd = False # Was compiler specificed from command line?
        self.release = True
        self.debug   = False
        # valid architectures: x86, x64, arm, arm64
        self.arch    = None
        self.jobs    = multiprocessing.cpu_count()
        self.target  = None
        self.flags   = None
        self.open    = None
        self.fortran = ''
        self.ios_version   = '11.0'
        self.macos_version = '10.12'
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
        ## Convenient installation utils:
        self.convenient_install = []
        ## Workspace and parsing
        self.global_workspace = False
        if System.windows:
            self.workspaces_root = forward_slashes(os.path.abspath(os.getenv('HOMEPATH')))
        else:
            self.workspaces_root = os.getenv('HOME')
        self.unused_args = []
        self.parse_args(args)
        self.check_platform()


    def set_platform(self, windows=False, linux=False, macos=False, \
                           ios=False, android=False, raspi=False):
        self.windows = windows
        self.linux   = linux
        self.macos   = macos
        self.ios     = ios
        self.android = android
        self.raspi   = raspi
        return True


    def is_platform_set(self):
        return self.windows or self.linux or self.macos \
            or self.ios or self.android or self.raspi
    
    
    def check_platform(self):
        if not self.is_platform_set():
            self.set_platform(windows=System.windows, linux=System.linux, macos=System.macos)
            if not self.is_platform_set():
                raise RuntimeError(f'Unsupported platform {sys.platform}: Please specify platform!')
        ## Arch itself is validated in set_arch(), however we need to validate if arch is allowed on platform
        if self.arch:
            if self.linux and 'arm' in self.arch:
                raise RuntimeError(f'Unsupported arch={self.arch} on linux platform! Build with android instead')
            if self.raspi and self.arch != 'arm':
                raise RuntimeError(f'Unsupported arch={self.arch} on raspi platform!')
            

    def set_arch(self, arch):
        arches = ['x86', 'x64', 'arm', 'arm64']
        if not arch in arches:
            raise RuntimeError(f"Unrecognized architecture {arch}! Valid options are: {arches}")
        self.arch = arch


    def is_64bit_build(self):
        return (self.arch == 'x64' or self.arch == 'arm64') \
            or (self.arch is None and System.is_64bit)

    
    def name(self):
        if self.windows: return 'windows'
        if self.linux:   return 'linux'
        if self.macos:   return 'macos'
        if self.ios:     return 'ios'
        if self.android: return 'android'
        if self.raspi:   return 'raspi'
        return 'build'


    def build_folder(self):
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
        return 'build'


    def set_build_config(self, release=False, debug=False):
        self.release = release
        self.debug   = debug
        return True


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


    def parse_args(self, args):
        for arg in args:
            if   arg == 'build':     self.build   = True
            elif arg == 'clean':     self.clean   = True
            elif arg == 'rebuild':   self.rebuild = True
            elif arg == 'update':    self.update  = True
            elif arg == 'deploy':    self.deploy  = True
            elif arg == 'serve':
                self.build = True
                self.update = True
                self.deploy = True
            elif arg == 'reclone':
                console('WARNING: Argument `reclone` is deprecated, use `wipe` instead.')
                self.reclone = True
            elif arg == 'wipe':      self.reclone   = True
            elif arg == 'init':      self.mama_init = True
            elif arg == 'silent':    self.print   = False
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
            elif arg == 'x86':     self.set_arch('x86')
            elif arg == 'x64':     self.set_arch('x64')
            elif arg == 'arm':     self.set_arch('arm')
            elif arg == 'arm64':   self.set_arch('arm64')
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
            elif arg.startswith('open='):   self.open = arg[5:]
            elif arg.startswith('jobs='):   self.jobs = int(arg[5:])
            elif arg.startswith('target='): self.target = arg[7:]
            elif arg.startswith('test='):   self.add_test_arg(arg[5:])
            elif arg.startswith('start='):  self.start = arg[6:]
            elif arg.startswith('arch='):   self.set_arch(arg[5:])
            elif arg.startswith('flags='):  self.flags = arg[6:]
            elif arg.startswith('android-'):
                self.set_platform(android=True)
                self.android_api = arg
            elif arg == 'install-clang6':  self.convenient_install.append('clang6')
            elif arg == 'install-clang11': self.convenient_install.append('clang11')
            elif arg == 'install-msbuild': self.convenient_install.append('msbuild')
            else:
                self.unused_args.append(arg)
            continue


    def add_test_arg(self, arg):
        if arg[0] == '"' and arg[-1] == '"':
            arg = arg[1:-1]
        if self.test: self.test += ' '
        self.test += arg


    def find_compiler_root(self, compiler):
        roots = ['/etc/alternatives/', '/usr/bin/', '/usr/local/bin/']
        for root in roots:
            if os.path.exists(root + compiler):
                #console(f'Mama compiler: {root}{compiler}')
                return root
        raise EnvironmentError(f'Could not find {compiler} from {roots}')


    def get_preferred_compiler_paths(self, cxx_enabled):
        if self.raspi:  # only GCC available for this platform
            ext = '.exe' if System.windows else ''
            cc  = f'{self.raspi_bin()}arm-linux-gnueabihf-gcc{ext}'
            cxx = f'{self.raspi_bin()}arm-linux-gnueabihf-g++{ext}'
            return (cc, cxx)
        if self.clang:
            key = 'clang++' if cxx_enabled else 'clang'
            if not self.clang_path: self.clang_path = self.find_compiler_root(key)
            cc = f'{self.clang_path}clang'
            cxx = f'{self.clang_path}clang++'
            return (cc, cxx)
        if self.gcc:
            key = 'g++' if cxx_enabled else 'gcc'
            if not self.gcc_path: self.gcc_path = self.find_compiler_root(key)
            cc = f'{self.gcc_path}gcc'
            cxx = f'{self.gcc_path}g++'
            return (cc, cxx)
        raise EnvironmentError('No preferred compiler for this platform!')
        

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


    def find_default_fortran_compiler(self):
        paths = []
        if System.linux:
            paths += [find_executable_from_system('gfortran')]
        
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

        vswhere = '"C:\\Program Files (x86)\\Microsoft Visual Studio\\Installer\\vswhere.exe" -latest -nologo -property installationPath'
        paths = [execute_piped(vswhere)]
        vs_variants = [ 'Enterprise', 'Professional', 'Community'  ]
        vs_versions = [ '2019', '2017' ]
        for version in vs_versions:
            for variant in vs_variants:
                paths.append(f'C:\\Program Files (x86)\\Microsoft Visual Studio\\{version}\\{variant}')

        for path in paths:
            if path and os.path.exists(path):
                #path = forward_slashes(path)
                self._visualstudio_path = path
                if self.verbose: console(f'Detected VisualStudio: {path}')
                return path

        return self._visualstudio_path
    

    def is_target_arch_x64(self):
        return self.arch == 'x64' or (System.is_64bit and not self.arch)


    def is_target_arch_x86(self):
        return self.arch == 'x86' or (not System.is_64bit and not self.arch)


    def is_target_arch_arm64(self):
        return self.arch == 'arm64' or (not self.arch)


    def is_target_arch_armv7(self):
        return self.arch == 'arm'


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
        if '\\2019\\' in path: self._visualstudio_cmake_id = 'Visual Studio 16 2019'
        else:                  self._visualstudio_cmake_id = 'Visual Studio 15 2017'
        
        if self.verbose: console(f'Detected CMake Generator: -G"{self._visualstudio_cmake_id}" -A {self.get_visualstudio_cmake_arch()}')
        return self._visualstudio_cmake_id


    def get_msbuild_path(self):
        if self._msbuild_path:
            return self._msbuild_path
        
        paths = [ find_executable_from_system('msbuild') ]
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
        clang_zip = download_file(f'http://ateh10.net/dev/{clangpp}-{suffix}.zip', tempfile.gettempdir())
        console(f'Installing to /usr/local/{clangpp}')
        execute(f'sudo rm -rf /usr/local/{clangpp}') # get rid of any old stuff
        execute(f'cd /usr/local && sudo unzip -oq {clang_zip}') # extract /usr/local/clang++11/
        os.remove(clang_zip)
        execute(f'sudo ln -sf /usr/local/{clangpp}/lib/libc++.so.1    /usr/lib')
        execute(f'sudo ln -sf /usr/local/{clangpp}/lib/libc++abi.so.1 /usr/lib')
        execute(f'sudo ln -sf /usr/local/{clangpp}/bin/clang      /usr/bin/clang-{clang_ver}')
        execute(f'sudo ln -sf /usr/local/{clangpp}/bin/clang++    /usr/bin/clang++-{clang_ver}')
        execute(f'sudo ln -sf /usr/local/{clangpp}/include/c++/v1 /usr/include/c++/v1')
        execute(f'sudo update-alternatives --install /usr/bin/clang   clang   /usr/bin/clang-{clang_ver}   100')
        execute(f'sudo update-alternatives --install /usr/bin/clang++ clang++ /usr/bin/clang++-{clang_ver} 100')
        execute(f'sudo update-alternatives --set clang   /usr/bin/clang-{clang_ver}')
        execute(f'sudo update-alternatives --set clang++ /usr/bin/clang++-{clang_ver}')


    def install_msbuild(self):
        if System.windows: raise OSError('Install Visual Studio 2019 to get MSBuild on Windows')
        if System.macos:   raise OSError('install_msbuild not implemented for macOS')
        
        dist = distro.info()
        if dist['id'] != "ubuntu": raise OSError('install_msbuild only supports Ubuntu')
        codename = dist['codename']

        execute('curl https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor > /tmp/microsoft.gpg')
        execute('sudo mv /tmp/microsoft.gpg /etc/apt/trusted.gpg.d/microsoft.gpg')
        execute(f"sudo sh -c 'echo \"deb [arch=amd64] https://packages.microsoft.com/repos/microsoft-ubuntu-{codename}-prod {codename} main\" > /etc/apt/sources.list.d/dotnetdev.list'")
        execute('sudo apt-get install apt-transport-https')
        execute('sudo apt-get update')
        execute('sudo apt-get install dotnet-sdk-2.1')


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

