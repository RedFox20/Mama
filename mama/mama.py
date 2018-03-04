#!/usr/bin/python3.6
import urllib.request, ssl, os.path, shutil, platform, glob, sys, zipfile
import ctypes, traceback, argparse, pathlib, random, subprocess, stat, time, shlex
from subprocess import run, STDOUT, PIPE, TimeoutExpired, Popen, CalledProcessError
import multiprocessing

## Always flush to properly support Jenkins
def console(s): print(s, flush=True)

if sys.version_info < (3, 6):
    console('FATAL ERROR: MamaBuild requires Python 3.6')
    exit(-1)

console("========= Mama Build Tool ==========\n")

class MamaSystem:
    def __init__(self):
        platform = sys.platform
        self.windows = platform == 'win32'
        self.linux   = platform.startswith('linux')
        self.macos   = platform == 'darwin'
        if not (self.windows or self.linux or self.macos):
            raise RuntimeError(f'Unsupported platform {platform}')
system = MamaSystem()

def find_executable_from_system(name):
    finder = 'where' if system.windows else 'which'
    output = subprocess.run([finder, name], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL).stdout.decode('utf-8')
    output = output.split('\n')[0].strip()
    return output if os.path.isfile(output) else ''

def print_usage():
    console('mama [actions...] [args...]')
    console('  actions:')
    console('    build     - update, configure and build main project or specific target')
    console('    clean     - clean main project or specific target')
    console('    rebuild   - clean, update, configure and build main project or specific target')
    console('    configure - run CMake configuration on main project or specific target')
    console('    reclone   - wipe specific target dependency and clone it again')
    console('    test      - run tests for main project or specific target')
    console('    add       - add new dependency')
    console('    new       - create new mama build file')
    console('  args:')
    console('    windows   - build for windows')
    console('    linux     - build for linux')
    console('    macos     - build for macos')
    console('    ios       - build for ios')
    console('    android   - build for android')
    console('    release   - (default) CMake configuration RelWithDebInfo')
    console('    debug     - CMake configuration Debug')
    console('    jobs=N    - Max number of parallel compilations. (default=system.core.count)')
    console('    target=P  - Name of the target')
    console('  examples:')
    console('    mama build                    Update and build main project only.')
    console('    mama clean                    Cleans main project only.')
    console('    mama rebuild                  Cleans, update and build main project only.')
    console('    mama build target=dep1        Update and build dep1 only.')
    console('    mama configure                Run CMake configuration on main project only.')
    console('    mama configure target=all     Run CMake configuration on main project and all deps.')
    console('    mama reclone target=dep1      Wipe target dependency completely and clone again.')
    console('    mama test                     Run tests on main project.')
    console('    mama test target=dep1         Run tests on target dependency project.')
    console('  environment:')
    console('    setenv("NINJA")               Path to NINJA build executable')
    console('    setenv("ANDROID_HOME")        Path to Android SDK if auto-detect fails')

###
# Mama Build Configuration is created only once in the root project working directory
# This configuration is then passed down to dependencies
#
class MamaBuildConfig:
    def __init__(self, args):
        self.build   = False
        self.clean   = False
        self.rebuild = False
        self.configure = False # re-run cmake configure
        self.reclone   = False
        self.test      = None
        self.windows = False
        self.linux   = False
        self.macos   = False
        self.ios     = False
        self.android = False
        self.release = True
        self.debug   = False
        self.jobs    = multiprocessing.cpu_count()
        self.target  = None
        self.ios_version   = '11.0'
        self.macos_version = '10.12'
        self.ninja_path = self.find_ninja_build()
        self.android_sdk_path = ''
        self.android_ndk_path = ''
        self.init_ndk_path()
        self.android_arch  = 'armeabi-v7a' # arm64-v8a
        self.android_tool  = 'arm-linux-androideabi-4.9' # aarch64-linux-android-4.9
        self.android_api   = 'android-24'
        self.android_ndk_stl = 'c++_shared' # LLVM libc++
        self.workspaces_root = os.getenv('HOMEPATH') if system.windows else os.getenv('HOME')
        for arg in args: self.parse_arg(arg)
        self.check_platform()
    def set_platform(self, windows=False, linux=False, macos=False, ios=False, android=False):
        self.windows = windows
        self.linux   = linux
        self.macos   = macos
        self.ios     = ios
        self.android = android
        return True
    def is_platform_set(self): return self.windows or self.linux or self.macos or self.ios or self.android
    def check_platform(self):
        if not self.is_platform_set():
            self.set_platform(windows=system.windows, linux=system.linux, macos=system.macos)
            if not self.is_platform_set():
                raise RuntimeError(f'Unsupported platform {sys.platform}: Please specify platform!')
    def name(self):
        if self.windows: return 'windows'
        if self.linux:   return 'linux'
        if self.macos:   return 'macos'
        if self.ios:     return 'ios'
        if self.android: return 'android'
        return 'build'
    def set_build_config(self, release=False, debug=False):
        self.release = release
        self.debug   = debug
        return True
    def parse_arg(self, arg):
        if   arg == 'build':     self.build   = True
        elif arg == 'clean':     self.clean   = True
        elif arg == 'rebuild':   self.rebuild = True
        elif arg == 'configure': self.configure = True
        elif arg == 'reclone':   self.reclone   = True
        elif arg == 'test':      self.test      = True
        elif arg == 'windows': self.set_platform(windows=True)
        elif arg == 'linux':   self.set_platform(linux=True)
        elif arg == 'macos':   self.set_platform(macos=True)
        elif arg == 'ios':     self.set_platform(ios=True)
        elif arg == 'android': self.set_platform(android=True)
        elif arg == 'release': self.set_build_config(release=True)
        elif arg == 'debug':   self.set_build_config(debug=True)
        elif arg.startswith('jobs='):   self.count = int(arg[5:])
        elif arg.startswith('target='): self.target = arg[7:]
        elif arg.startswith('test='):   self.test = arg[5:]
        elif arg == 'test':             self.test = ' '
    def find_ninja_build(self):
        ninja_executables = [
            os.getenv('NINJA'), 
            find_executable_from_system('ninja'),
            '/Projects/ninja.exe'
        ]
        for ninja_exe in ninja_executables:        
            if ninja_exe and os.path.isfile(ninja_exe):
                console(f'Found Ninja Build System: {ninja_exe}')
                return ninja_exe
        return ''
    def init_ndk_path(self):
        androidenv = os.getenv('ANDROID_HOME')
        paths = [androidenv] if androidenv else []
        if system.windows: paths += [f'{os.getenv("LOCALAPPDATA")}\\Android\\Sdk']
        elif system.linux: paths += ['/usr/bin/android-sdk', '/opt/android-sdk']
        elif system.macos: paths += [f'{os.getenv("HOME")}/Library/Android/sdk']
        ext = '.cmd' if system.windows else ''
        for sdk_path in paths:
            if os.path.exists(f'{sdk_path}/ndk-bundle/ndk-build{ext}'):
                self.android_sdk_path = sdk_path
                self.ndk_path = sdk_path  + '/ndk-bundle'
                console(f'Found Android NDK: {self.ndk_path}')
                return
        return ''
    def libname(self, library):
        if self.windows: return f'{library}.lib'
        else:            return f'lib{library}.a'
    def libext(self):
        return 'lib' if self.windows else 'a'
######################################################################################

def is_file_modified(src, dst):
    return os.path.getmtime(src) == os.path.getmtime(dst) and\
           os.path.getsize(src) == os.path.getsize(dst)

def copy_files(fromFolder, toFolder, fileNames):
    for file in fileNames:
        sourceFile = os.path.join(fromFolder, file)
        if not os.path.exists(sourceFile):
            continue
        destFile = os.path.join(toFolder, os.path.basename(file))
        destFileExists = os.path.exists(destFile)
        if destFileExists and is_file_modified(sourceFile, destFile):
            console(f"skipping copy '{destFile}'")
            continue
        console(f"copyto '{toFolder}'  '{sourceFile}'")
        if system.windows and destFileExists: # note: windows crashes if dest file is in use
            tempCopy = f'{destFile}.{random.randrange(1000)}.deleted'
            shutil.move(destFile, tempCopy)
            try:
                os.remove(tempCopy)
            except Exception:
                pass
        shutil.copy2(sourceFile, destFile) # copy while preserving metadata

def execute(command):
    if os.system(command) != 0:
        raise Exception(f'{command} failed')

######################################################################################

class MamaDependency:
    def __init__(self, config, name, workspace, git_url='', branch='', tag=''):
        self.config = config
        self.name = name
        self.workspace  = workspace
        self.git_url    = git_url
        self.git_branch = branch
        self.git_tag    = tag
        self.dependency_folder = os.path.join(config.workspaces_root, workspace, self.dependency_name())
        self.source_folder     = os.path.join(self.dependency_folder, name)
        self.build_folder      = os.path.join(self.dependency_folder, config.name())
    def dependency_name(self):
        if self.git_branch: return f'{self.name}-{self.git_branch}'
        if self.git_tag:    return f'{self.name}-{self.git_tag}'
        return self.name

######################################################################################

class MamaBuildTarget(MamaDependency):
    def __init__(self, name, workspace, git_url='', branch='', tag='', config=None):
        if config is None:
            raise RuntimeError('MamaBuildTarget config argument must be set')
        super().__init__(self, config, name, workspace, git_url, branch, tag)
        self.install_folder   = './'
        self.install_target   = 'install'
        self.build_dependency = ''
        self.cmake_ndk_toolchain = f'{config.ndk_path}/build/cmake/android.toolchain.cmake'
        self.cmake_ios_toolchain = ''
        self.cmake_opts       = []
        self.cmake_cxxflags   = ''
        self.cmake_ldflags    = ''
        self.cmake_build_type = 'Debug' if config.debug else 'RelWithDebInfo'
        self.enable_exceptions = True
        self.enable_unix_make  = False
        self.enable_ninja_build = True and config.ninja_path # attempt to use Ninja
        self.enable_multiprocess_build = True

    def cmake_generator(self):
        def choose_gen():
            if self.enable_unix_make:   return '-G "CodeBlocks - Unix Makefiles"'
            if self.config.windows:     return '-G "Visual Studio 15 2017 Win64"'
            if self.enable_ninja_build: return '-G "Ninja"'
            if self.config.android:     return '-G "CodeBlocks - Unix Makefiles"'
            if self.config.linux:       return '-G "CodeBlocks - Unix Makefiles"'
            if self.config.ios:         return '-G "Xcode"'
            if self.config.macos:       return '-G "Xcode"'
            else:                       return ''
        return choose_gen()
    
    def mp_flags(self):
        if not self.enable_multiprocess_build: return ''
        if self.config.windows:     return f'/maxcpucount:{self.config.jobs}'
        if self.enable_unix_make:   return f'-j {self.config.jobs}'
        if self.enable_ninja_build: return ''
        if self.config.ios:         return f'-jobs {self.config.jobs}'
        if self.config.macos:       return f'-jobs {self.config.jobs}'
        return f'-j {self.config.jobs}'
    
    def buildsys_flags(self):
        def get_flags():
            if self.config.windows:     return f'/v:m {self.mp_flags()} '
            if self.enable_unix_make:   return self.mp_flags()
            if self.enable_ninja_build: return ''
            if self.config.android:     return self.mp_flags()
            if self.config.ios:         return f'-quiet {self.mp_flags()}'
            if self.config.macos:       return f'-quiet {self.mp_flags()}'
            return self.mp_flags()
        flags = get_flags()
        return f'-- {flags}' if flags else ''

    def cmake_make_program(self):
        if self.config.windows:     return ''
        if self.enable_unix_make:   return ''
        if self.enable_ninja_build: return self.config.ninja_path
        if self.config.android:
            if system.windows:
                return f'{self.config.ndk_path}\\prebuilt\\windows-x86_64\\bin\\make.exe' # CodeBlocks - Unix Makefiles
            elif system.macos:
                return f'{self.config.ndk_path}/prebuilt/darwin-x86_64/bin/make' # CodeBlocks - Unix Makefiles
        return ''

    def cmake_default_options(self):
        cxxflags = self.cmake_cxxflags
        ldflags  = self.cmake_ldflags
        if self.config.windows:
            cxxflags += ' /EHsc -D_HAS_EXCEPTIONS=1' if self.enable_exceptions else ' -D_HAS_EXCEPTIONS=0'
            cxxflags += ' -DWIN32=1' # so yeah, only _WIN32 is defined by default, but opencv wants to see WIN32
            cxxflags += ' /MP'
        else:
            cxxflags += '' if self.enable_exceptions else ' -fno-exceptions'
        
        if self.config.android and self.config.android_ndk_stl == 'c++_shared':
            cxxflags += f' -I"{self.config.ndk_path}/sources/cxx-stl/llvm-libc++/include" '
        elif self.config.linux or self.config.macos:
            cxxflags += ' -march=native -stdlib=libc++ '
        elif self.config.ios:
            cxxflags += f' -arch arm64 -stdlib=libc++ -miphoneos-version-min={self.config.ios_version} '

        opt = [f"CMAKE_BUILD_TYPE={self.cmake_build_type}",
                "CMAKE_POSITION_INDEPENDENT_CODE=ON"]
        if cxxflags: opt += [f'CMAKE_CXX_FLAGS="{cxxflags}"']
        if ldflags: opt += [
            f'CMAKE_EXE_LINKER_FLAGS="{ldflags}"',
            f'CMAKE_MODULE_LINKER_FLAGS="{ldflags}"',
            f'CMAKE_SHARED_LINKER_FLAGS="{ldflags}"',
            f'CMAKE_STATIC_LINKER_FLAGS="{ldflags}"'
        ]
        make = self.cmake_make_program()
        if make: opt.append(f'CMAKE_MAKE_PROGRAM="{make}"')

        if self.config.android:
            opt += [
                'BUILD_ANDROID=ON',
                'TARGET_ARCH=ANDROID',
                'CMAKE_SYSTEM_NAME=Android',
                f'ANDROID_ABI={self.config.android_arch}',
                'ANDROID_ARM_NEON=TRUE',
                f'ANDROID_NDK="{self.config.ndk_path}"',
                f'NDK_DIR="{self.config.ndk_path}"',
                'NDK_RELEASE=r16b',
                f'ANDROID_NATIVE_API_LEVEL={self.config.android_api}',
                'CMAKE_BUILD_WITH_INSTALL_RPATH=ON',
                f'ANDROID_STL={self.config.android_ndk_stl}',
                'ANDROID_TOOLCHAIN=clang'
            ]
            if self.cmake_ndk_toolchain:
                opt += [f'CMAKE_TOOLCHAIN_FILE="{self.cmake_ndk_toolchain}"']
        elif self.config.ios:
            opt += [
                'IOS_PLATFORM=OS',
                'CMAKE_SYSTEM_NAME=Darwin',
                'CMAKE_XCODE_EFFECTIVE_PLATFORMS=-iphoneos',
                'CMAKE_OSX_ARCHITECTURES=arm64',
                'CMAKE_VERBOSE_MAKEFILE=OFF',
                'CMAKE_OSX_SYSROOT="/Applications/Xcode.app/Contents/Developer/Platforms/iPhoneOS.platform/Developer/SDKs/iPhoneOS.sdk"'
            ]
            if self.cmake_ios_toolchain:
                opt += [f'CMAKE_TOOLCHAIN_FILE="{self.cmake_ios_toolchain}"']
        return opt

    def inject_env(self):
        if self.config.android:
            make = self.cmake_make_program()
            if make: os.environ['CMAKE_MAKE_PROGRAM'] = make
            os.environ['ANDROID_HOME'] = self.config.android_sdk_path
            os.environ['ANDROID_NDK'] = self.config.ndk_path
            os.environ['ANDROID_ABI'] = self.config.android_arch
            os.environ['NDK_RELEASE'] = 'r15c'
            os.environ['ANDROID_STL'] = self.config.android_ndk_stl
            os.environ['ANDROID_NATIVE_API_LEVEL'] = self.config.android_api
            #os.environ['ANDROID_TOOLCHAIN_NAME']   = self.android_tool
            os.environ['ANDROID_TOOLCHAIN']        = 'clang'
        elif self.config.ios:
            os.environ['IPHONEOS_DEPLOYMENT_TARGET'] = self.config.ios_version
        elif self.config.macos:
            os.environ['MACOSX_DEPLOYMENT_TARGET'] = self.config.macos_version

    def get_cmake_flags(self):
        flags = ''
        options = self.cmake_opts + self.cmake_default_options()
        for opt in options: flags += '-D'+opt+' '
        return flags

    def add_cxx_flags(self, msvc='', clang=''):
        self.cmake_cxxflags += ' '
        self.cmake_cxxflags += msvc if self.config.windows else clang

    def add_linker_flags(self, windows='', android='', ios='', linux='', mac=''):
        flags = self.select(windows, android, ios, linux, mac)
        if flags: self.cmake_ldflags += ' '+flags

    def add_cmake_options(self, *options):
        for option in options:
            if isinstance(option, list): self.cmake_opts += option
            else:                        self.cmake_opts.append(option)

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

    def make_build_subdir(self, subdir):
        os.makedirs(f'{self.build_folder}/{subdir}')

    def copy_built_file(self, builtFile, copyToFolder):
        shutil.copy(f'{self.build_folder}/{builtFile}', f'{self.build_folder}/{copyToFolder}')


    def set_dependency(self, all='', windows='', android='', ios='', linux='', mac=''):
        dependency = all if all else self.select(windows, android, ios, linux, mac)
        if dependency: self.build_dependency = os.path.join(self.build_folder, dependency)

    def download_file(self, remote_url, local_dir, force=False):
        local_file = os.path.join(local_dir, os.path.basename(remote_url))
        if not force and os.path.exists(local_file): # download file?
            console(f"Using locally cached {local_file}")
            return local_file
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with urllib.request.urlopen(remote_url, context=ctx) as urlfile:
            with open(local_file, 'wb') as output:
                total = int(urlfile.info()['Content-Length'].strip())
                total_megas = int(total/(1024*1024))
                prev_progress = -100
                written = 0
                while True:
                    data = urlfile.read(32*1024) # large chunks plz
                    if not data:
                        console(f"\rDownload {remote_url} finished.                 ")
                        return local_file
                    output.write(data)
                    written += len(data)
                    progress = int((written*100)/total)
                    if (progress - prev_progress) >= 5: # report every 5%
                        prev_progress = progress
                        written_megas = int(written/(1024*1024))
                        print(f"\rDownloading {remote_url} {written_megas}/{total_megas}MB ({progress}%)...")


    def download_and_unzip(self, remote_zip, extract_dir):
        local_file = self.download_file(remote_zip, extract_dir)
        with zipfile.ZipFile(local_file, "r") as zip:
            zip.extractall(extract_dir)


    def run(self, command):
        console(command)
        execute(command)

    def run_cmake(self, cmake_command): self.run(f"cd {self.build_folder} && cmake {cmake_command}")
    def run_git(self, git_command):     self.run(f"cd {self.source_folder} && git {git_command}")

    @staticmethod
    def is_dir_empty(dir): # no files?
        if not os.path.exists(dir): return True
        _, _, filenames = next(os.walk(dir))
        return len(filenames) == 0

    def should_clone(self):
        return self.git_url and self.is_dir_empty(self.source_folder)

    def should_rebuild(self):
        return not self.build_dependency or not os.path.exists(self.build_dependency) or self.git_commit_changed()

    def should_clean(self):
        return self.build_folder != '/' and os.path.exists(self.build_folder)

    def git_tag_save(self):
        pathlib.Path(f"{self.build_folder}/git_tag").write_text(self.git_tag)

    @staticmethod
    def has_tag_changed(old_tag_file, new_tag):
        if not os.path.exists(old_tag_file):
            return True
        old_tag = pathlib.Path(old_tag_file).read_text()
        if old_tag != new_tag:
            console(f" tagchange '{old_tag.strip()}'\n"+
                    f"      ---> '{new_tag.strip()}'")
            return True
        return False

    def git_tag_changed(self):
        return self.has_tag_changed(f"{self.build_folder}/git_tag", self.git_tag)

    def git_current_commit(self): 
        cp = run(['git','show','--oneline','-s'], stdout=PIPE, cwd=self.source_folder)
        return cp.stdout.decode('utf-8')

    def git_commit_save(self):
        pathlib.Path(f"{self.build_folder}/git_commit").write_text(self.git_current_commit())

    def git_commit_changed(self):
        return self.has_tag_changed(f"{self.build_folder}/git_commit", self.git_current_commit())
    
    def checkout_current_branch(self):
        branch = self.git_branch if self.git_branch else self.git_tag
        if branch:
            if self.git_tag and self.git_tag_changed():
                self.run_git("reset --hard")
                self.git_tag_save()
            self.run_git(f"checkout {branch}")

    def clone(self):
        if self.config.reclone and self.config.target:
            console(f'Reclone wipe {self.dependency_folder}')
            if os.path.exists(self.dependency_folder):
                if system.windows: # chmod everything to user so we can delete:
                    for root, dirs, files in os.walk(self.dependency_folder):
                        for d in dirs:  os.chmod(os.path.join(root, d), stat.S_IWUSR)
                        for f in files: os.chmod(os.path.join(root, f), stat.S_IWUSR)
                shutil.rmtree(self.dependency_folder)

        if self.should_clone():
            console('\n\n#############################################################')
            console(f"Cloning {self.name} ...")
            execute(f"git clone {self.git_url} {self.source_folder}")
            self.checkout_current_branch()
        elif self.git_url:
            console(f'Pulling {self.name} ...')
            self.checkout_current_branch()
            if not self.git_tag: # never pull a tag
                self.run_git("reset --hard")
                self.run_git("pull")

    def configure(self, reconfigure=False):
        if not self.should_rebuild() and not reconfigure:
            return False
        console('\n\n#############################################################')
        console(f"Configuring {self.name} ...")
        if not os.path.exists(self.build_folder): os.mkdir(self.build_folder)
        flags = self.get_cmake_flags()
        gen = self.cmake_generator()
        self.run_cmake(f"{gen} {flags} -DCMAKE_INSTALL_PREFIX={self.install_folder} . ../")
        return True

    def build(self, install=True, reconfigure=False):
        if self.config.clean:
            self.clean()
        if self.configure(reconfigure):
            console('\n\n#############################################################')
            console(f"Building {self.name} ...")
            self.inject_env()
            self.run_cmake(f"--build . --config {self.cmake_build_type} {self.prepare_install_target(install)} {self.buildsys_flags()}")
            self.git_commit_save()
        else:
            console(f'{self.name} already built {self.build_dependency}')


    def prepare_install_target(self, install):
        if not self.install_target or not install:
            return ''
        os.makedirs(f"{self.build_folder}/{self.install_folder}", exist_ok=True)
        return f'--target {self.install_target}'

    def install(self):
        if self.should_rebuild():
            console('\n\n#############################################################')
            console(f"Installing {self.name} ...")
            self.run_cmake(f"--build . --config {self.cmake_build_type} {self.prepare_install_target(True)}")

    def clean(self):
        if self.should_clean():
            console('\n\n#############################################################')
            console(f"Cleaning {self.name} ... {self.build_folder}")
            #self.run_cmake("--build . --target clean")
            shutil.rmtree(self.build_folder, ignore_errors=True)

    def clone_build_install(self, reconfigure=False):
        self.clone()
        self.build(reconfigure=self.config.configure)


######################################################################################

def deploy_framework(framework, deployFolder):
    if not os.path.exists(framework):
        raise IOError(f'no framework found at: {framework}') 
    if os.path.exists(deployFolder):
        name = os.path.basename(framework)
        deployPath = os.path.join(deployFolder, name)
        console(f'Deploying framework to {deployPath}')
        execute(f'rm -rf {deployPath}')
        shutil.copytree(framework, deployPath)
        return True
    return False

def run_with_timeout(executable, argstring, workingDir, timeoutSeconds=None):
    args = [executable]
    args += shlex.split(argstring)
    start = time.time()
    proc = Popen(args, shell=True, cwd=workingDir)
    try:
        proc.wait(timeout=timeoutSeconds)
        console(f'{executable} elapsed: {round(time.time()-start, 1)}s')
    except TimeoutExpired:
        console('TIMEOUT, sending break signal')
        if system.windows:
            proc.send_signal(subprocess.signal.CTRL_C_EVENT)
        else:
            proc.send_signal(subprocess.signal.SIGINT)
        raise
    if proc.returncode == 0:
        return
    raise CalledProcessError(proc.returncode, ' '.join(args))

        