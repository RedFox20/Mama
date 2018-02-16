#!/usr/bin/python3.6
import urllib.request, ssl, os.path, shutil, platform, glob, sys, zipfile
import ctypes, traceback, argparse, pathlib, random, subprocess, stat
from subprocess import run, STDOUT, PIPE, TimeoutExpired, Popen, CalledProcessError

if sys.version_info < (3, 6):
    print('FATAL ERROR: MamaBuild requires Python 3.6', flush=True)
    exit(-1)

print("========= Mama Build Tool ==========\n")
sys.stdout.flush()
parser = argparse.ArgumentParser(prog='build.py')
parser.add_argument('--android', action='store_true', help='build for android')
parser.add_argument('--ios',     action='store_true', help='build of ios')
parser.add_argument('--linux',   action='store_true', help='build of linux')
parser.add_argument('--mac',     action='store_true', help='build of mac')
parser.add_argument('--windows', action='store_true', help='build of windows')
parser.add_argument('--update',  action='store_true', help='force update all dependency repositories')
parser.add_argument('--cmake',   action='store_true', help='force cmake configure on target project(s)')
parser.add_argument('--debug',   action='store_true', help='force cmake to use Debug instead of RelWithDebInfo')
parser.add_argument('--clean',   action='store_true', help='clean all cmake build files and binaries for currently selected platform')
parser.add_argument('--target',  help='build/clean a specific target: eigen ceres opencv cares curl aws zip glfw facewolf')
parser.add_argument('--tests',   nargs='*', help='run unit tests for FaceWolf with the following args')
parser.add_argument('--nogitpull',action='store_true', help='disables git pull on dependency repos, does not affect SFM or RPP')
parser.add_argument('--nopkg',    action='store_true', help='disables any unix package updates')
parser.add_argument('--reclone',  action='store_true', help='wipes target dependency directory and does a fresh clone (only valid when using --target x)')
parser.add_argument('--jobs',    type=int, default=7,  help='sets the number of build jobs (default=7)')
args = parser.parse_args()
parser.print_help()
cwd = os.getcwd().replace('\\', '/')

######################################################################################

def console(s):
    print(s, flush=True)

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
        if args.windows and destFileExists: # note: windows crashes if dest file is in use
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

def os_macos():   return sys.platform == "darwin"
def os_linux():   return sys.platform == "linux" or sys.platform == "linux2"
def os_windows(): return sys.platform == "win32"

# first checks $ANDROID_NDK envvar then tries to guess possible NDK paths
def init_android_path():
    androidenv = os.getenv('ANDROID_HOME')
    paths = [androidenv] if androidenv else []
    if os_windows(): paths += [f'{os.getenv("LOCALAPPDATA")}\\Android\\Sdk']
    elif os_linux(): paths += ['/usr/bin/android-sdk', '/opt/android-sdk']
    elif os_macos(): paths += [f'{os.getenv("HOME")}/Library/Android/sdk']
    ext = '.cmd' if os_windows() else ''
    for sdk_path in paths:
        if os.path.exists(f'{sdk_path}/ndk-bundle/ndk-build{ext}'):
            console(f'Found Android SDK: {sdk_path}')
            return (sdk_path, sdk_path + '/ndk-bundle')
    return ('', '')
    console(f'ERROR: Failed to detect Android NDK. Try setting env ANDROID_HOME')
    exit(-1)
android_sdk_path = init_android_path() if args.android else ''
ndk_path = android_sdk_path + '/ndk-bundle' if android_sdk_path else ''

def has_tag_changed(old_tag_file, new_tag):
    if not os.path.exists(old_tag_file):
        return True
    old_tag = pathlib.Path(old_tag_file).read_text()
    if old_tag != new_tag:
        console(f" tagchange '{old_tag.strip()}'\n"+
                f"      ---> '{new_tag.strip()}'")
        return True
    return False

######################################################################################

class MamaDependency:
    def __init__(self):
        pass

######################################################################################

class MamaBuild:
    """Mama build and dependency manager"""
     # some configuration must be static
    android_arch  = 'armeabi-v7a' # arm64-v8a
    android_tool  = 'arm-linux-androideabi-4.9' # aarch64-linux-android-4.9
    android_api   = 'android-24'
    ios_version   = '11.0'
    macos_version = '10.12'
    cmake_ndk_stl = 'c++_shared' # LLVM libc++
    ninja_path    = ''
    ndk_path      = ''
    def __init__(self, name, project_folder, git_url='', branch='', tag=''):
        self.name             = name
        self.project_folder   = project_folder
        self.build_folder     = os.path.join(project_folder, self.build_name())
        self.git_url          = git_url
        self.git_branch       = branch
        self.git_tag          = tag
        self.install_folder   = './'
        self.install_target   = 'install'
        self.build_dependency = ''
        self.find_ndk_path()
        self.cmake_ndk_toolchain = f'{ndk_path}/build/cmake/android.toolchain.cmake'
        self.cmake_ios_toolchain = ''
        self.cmake_opts       = []
        self.cmake_cxxflags   = ''
        self.cmake_ldflags    = ''
        self.cmake_build_type = 'Debug' if args.debug else 'RelWithDebInfo'
        self.enable_exceptions = True
        self.enable_unix_make  = False
        self.enable_ninja_build = True and self.find_ninja_build() # attempt to use Ninja
        self.enable_multiprocess_build = True

    @staticmethod
    def find_executable_from_system(name):
        finder = 'where' if os_windows() else 'which'
        output = subprocess.run([finder, 'ninja'], stdout=subprocess.PIPE).stdout.decode('utf-8')
        output = output.split('\n')[0].strip()
        return output if os.path.isfile(output) else ''

    @staticmethod
    def find_ninja_build():
        if MamaBuild.ninja_path:
            return MamaBuild.ninja_path
        ninja_executables = [
            os.getenv('NINJA'), 
            MamaBuild.find_executable_from_system('ninja'),
            '/Projects/ninja'
        ]
        for ninja_exe in ninja_executables:        
            if ninja_exe and os.path.isfile(ninja_exe):
                console(f'Found Ninja Build System: {ninja_exe}')
                MamaBuild.ninja_path = ninja_exe
                return ninja_exe
        return ''

    @staticmethod
    def find_ndk_path():
        if MamaBuild.ndk_path:
            return MamaBuild.ndk_path
        androidenv = os.getenv('ANDROID_HOME')
        paths = [androidenv] if androidenv else []
        if os_windows(): paths += [f'{os.getenv("LOCALAPPDATA")}\\Android\\Sdk']
        elif os_linux(): paths += ['/usr/bin/android-sdk', '/opt/android-sdk']
        elif os_macos(): paths += [f'{os.getenv("HOME")}/Library/Android/sdk']
        ext = '.cmd' if os_windows() else ''
        for sdk_path in paths:
            if os.path.exists(f'{sdk_path}/ndk-bundle/ndk-build{ext}'):
                MamaBuild.ndk_path = sdk_path  + '/ndk-bundle'
                console(f'Found Android NDK: {MamaBuild.ndk_path}')
                return MamaBuild.ndk_path
        return ''

    def build_name(self):
        if args.android: return "android"
        if args.linux:   return "linux"
        if args.ios:     return "ios"
        if args.windows: return "windows"
        if args.mac:     return "mac"
        return "build"

    def cmake_generator(self):
        def choose_gen():
            if self.enable_unix_make:  return '-G "CodeBlocks - Unix Makefiles"'
            if args.windows:           return '-G "Visual Studio 15 2017 Win64"'
            if self.enable_ninja_build:return '-G "Ninja"'
            if args.android:           return '-G "CodeBlocks - Unix Makefiles"'
            if args.linux:             return '-G "CodeBlocks - Unix Makefiles"'
            if args.ios or args.mac:   return '-G "Xcode"'
            else:                      return ''
        return choose_gen()
    
    def mp_flags(self):
        if not self.enable_multiprocess_build: return ''
        if args.windows:             return f'/maxcpucount:{args.jobs}'
        if self.enable_unix_make:    return f'-j {args.jobs}'
        if self.enable_ninja_build:  return ''
        if args.ios or args.mac:     return f'-jobs {args.jobs}'
        return f'-j {args.jobs}'
    
    def buildsys_flags(self):
        def get_flags():
            if args.windows:             return f'/v:m {self.mp_flags()} '
            if self.enable_unix_make:    return self.mp_flags()
            if self.enable_ninja_build:  return ''
            if args.android:             return self.mp_flags()
            if args.ios or args.mac:     return f'-quiet {self.mp_flags()}'
            return self.mp_flags()
        flags = get_flags()
        return f'-- {flags}' if flags else ''

    def cmake_make_program(self):
        if args.windows:            return ''
        if self.enable_unix_make:   return ''
        if self.enable_ninja_build: return MamaBuild.ninja_path
        if args.android:
            if os_windows():
                return f'{ndk_path}\\prebuilt\\windows-x86_64\\bin\\make.exe' # CodeBlocks - Unix Makefiles
            elif os_macos():
                return f'{ndk_path}/prebuilt/darwin-x86_64/bin/make' # CodeBlocks - Unix Makefiles
        return ''

    def cmake_default_options(self):
        cxxflags = self.cmake_cxxflags
        ldflags  = self.cmake_ldflags
        if args.windows:
            cxxflags += ' /EHsc -D_HAS_EXCEPTIONS=1' if self.enable_exceptions else ' -D_HAS_EXCEPTIONS=0'
            cxxflags += ' -DWIN32=1' # so yeah, only _WIN32 is defined by default, but opencv wants to see WIN32
            cxxflags += ' /MP'
        else:
            cxxflags += '' if self.enable_exceptions else ' -fno-exceptions'
        
        if args.android and self.cmake_ndk_stl == 'c++_shared':
            cxxflags += f' -I"{ndk_path}/sources/cxx-stl/llvm-libc++/include" '
        elif args.linux or args.mac:
            cxxflags += ' -march=native -stdlib=libc++ '
        elif args.ios:
            cxxflags += f' -arch arm64 -stdlib=libc++ -miphoneos-version-min={self.ios_version} '

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

        if args.android:
            opt += [
                'BUILD_ANDROID=ON',
                'TARGET_ARCH=ANDROID',
                'CMAKE_SYSTEM_NAME=Android',
                f'ANDROID_ABI={self.android_arch}',
                'ANDROID_ARM_NEON=TRUE',
                f'ANDROID_NDK="{ndk_path}"',
                f'NDK_DIR="{ndk_path}"',
                'NDK_RELEASE=r16b',
                f'ANDROID_NATIVE_API_LEVEL={self.android_api}',
                'CMAKE_BUILD_WITH_INSTALL_RPATH=ON',
                f'ANDROID_STL={self.cmake_ndk_stl}',
                'ANDROID_TOOLCHAIN=clang'
            ]
            if self.cmake_ndk_toolchain:
                opt += [f'CMAKE_TOOLCHAIN_FILE="{self.cmake_ndk_toolchain}"']
        elif args.ios:
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
        if args.android:
            make = self.cmake_make_program()
            if make: os.environ['CMAKE_MAKE_PROGRAM'] = make
            os.environ['ANDROID_HOME'] = android_sdk_path
            os.environ['ANDROID_NDK'] = f"{android_sdk_path}/ndk-bundle"
            os.environ['ANDROID_ABI'] = self.android_arch
            os.environ['NDK_RELEASE'] = 'r15c'
            os.environ['ANDROID_STL'] = self.cmake_ndk_stl
            os.environ['ANDROID_NATIVE_API_LEVEL'] = self.android_api
            #os.environ['ANDROID_TOOLCHAIN_NAME']   = self.android_tool
            os.environ['ANDROID_TOOLCHAIN']        = 'clang'
        elif args.ios:
            os.environ['IPHONEOS_DEPLOYMENT_TARGET'] = self.ios_version
        elif args.mac:
            os.environ['MACOSX_DEPLOYMENT_TARGET'] = self.macos_version

    def get_cmake_flags(self):
        flags = ''
        options = self.cmake_opts + self.cmake_default_options()
        for opt in options: flags += '-D'+opt+' '
        return flags

    def add_cxx_flags(self, msvc='', clang=''):
        self.cmake_cxxflags += ' '
        self.cmake_cxxflags += msvc if args.windows else clang

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

    def select(self, windows, android, ios, linux, mac):
        if   args.android and android: return android
        elif args.ios     and ios:     return ios
        elif args.windows and windows: return windows
        elif args.linux   and linux:   return linux
        elif args.mac     and mac:     return mac
        return None

    def enable_cxx17(self):
        self.cmake_cxxflags += ' /std:c++17' if args.windows else ' -std=c++17'
    def enable_cxx14(self):
        self.cmake_cxxflags += ' /std:c++14' if args.windows else ' -std=c++14'
    def enable_cxx11(self):
        self.cmake_cxxflags += ' /std:c++11' if args.windows else ' -std=c++11'

    def make_build_subdir(self, subdir):
        os.makedirs(f'{self.build_folder}/{subdir}')

    def copy_built_file(self, builtFile, copyToFolder):
        shutil.copy(f'{self.build_folder}/{builtFile}', f'{self.build_folder}/{copyToFolder}')


    def set_dependency(self, all='', windows='', android='', ios='', linux='', mac=''):
        dependency = all if all else self.select(windows, android, ios, linux, mac)
        if dependency: self.build_dependency = os.path.join(self.build_folder, dependency)

    @staticmethod
    def print(str): # unbuffered print
        print(str, flush=True)

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
                        console(f"Download {remote_url} finished.                 ")
                        return local_file
                    output.write(data)
                    written += len(data)
                    progress = int((written*100)/total)
                    if (progress - prev_progress) >= 5: # report every 5%
                        prev_progress = progress
                        written_megas = int(written/(1024*1024))
                        print(f"\rDownloading {remote_url} {written_megas}/{total_megas}MB ({progress}%)...", end='\r')


    def download_and_unzip(self, remote_zip, extract_dir):
        local_file = self.download_file(remote_zip, extract_dir)
        with zipfile.ZipFile(local_file, "r") as zip:
            zip.extractall(extract_dir)


    def run(self, command):
        console(command)
        execute(command)

    def run_cmake(self, cmake_command): self.run(f"cd {self.build_folder} && cmake {cmake_command}")
    def run_git(self, git_command):     self.run(f"cd {self.project_folder} && git {git_command}")

    @staticmethod
    def is_dir_empty(dir): # no files?
        if not os.path.exists(dir): return True
        dirpath, dirnames, filenames = next(os.walk(dir))
        return len(filenames) == 0

    def should_clone(self):
        return self.git_url and self.is_dir_empty(self.project_folder)

    def should_rebuild(self):
        return not self.build_dependency or not os.path.exists(self.build_dependency) or self.git_commit_changed()

    def should_clean(self):
        return self.build_folder != '/' and os.path.exists(self.build_folder)

    def git_tag_save(self):
        pathlib.Path(f"{self.build_folder}/git_tag").write_text(self.git_tag)

    def git_tag_changed(self):
        return has_tag_changed(f"{self.build_folder}/git_tag", self.git_tag)

    def git_current_commit(self): 
        cp = run(['git','show','--oneline','-s'], stdout=PIPE, cwd=self.project_folder)
        return cp.stdout.decode('utf-8')

    def git_commit_save(self):
        pathlib.Path(f"{self.build_folder}/git_commit").write_text(self.git_current_commit())

    def git_commit_changed(self):
        return has_tag_changed(f"{self.build_folder}/git_commit", self.git_current_commit())
    
    def checkout_current_branch(self):
        branch = self.git_branch if self.git_branch else self.git_tag
        if branch:
            if self.git_tag and self.git_tag_changed():
                self.run_git("reset --hard")
                self.git_tag_save()
            self.run_git(f"checkout {branch}")

    def clone(self):
        if args.reclone and args.target:
            console(f'Reclone wipe {self.project_folder}')
            if os.path.exists(self.project_folder):
                if args.windows: # chmod everything to user so we can delete:
                    for root, dirs, files in os.walk(self.project_folder):
                        for d in dirs:  os.chmod(os.path.join(root, d), stat.S_IWUSR)
                        for f in files: os.chmod(os.path.join(root, f), stat.S_IWUSR)
                shutil.rmtree(self.project_folder)

        if self.should_clone():
            console('\n\n#############################################################')
            console(f"Cloning {self.name} ...")
            execute(f"git clone {self.git_url} {self.project_folder}")
            self.checkout_current_branch()
        elif self.git_url and not args.nogitpull:
            console(f'Pulling {self.name} ...')
            self.checkout_current_branch()
            if not self.git_tag: # never pull a tag
                self.run_git(f"reset --hard")
                self.run_git(f"pull")

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
        if args.clean:
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
            self.run_cmake(f"--build . --config {self.cmake_build_type} {self.prepare_install_target()}")

    def clean(self):
        if self.should_clean():
            console('\n\n#############################################################')
            console(f"Cleaning {self.name} ... {self.build_folder}")
            #self.run_cmake("--build . --target clean")
            shutil.rmtree(self.build_folder, ignore_errors=True)

    def clone_build_install(self, reconfigure=False):
        self.clone()
        self.build(reconfigure=args.cmake)


######################################################################################

def libname(library, version=''):
    if args.windows:
        return f"{library}{version}.lib"
    return f"lib{library}.a"

libext = "lib" if args.windows else "a"

######################################################################################

def build_facewolf_android(facewolf):
    os.utime("CMakeLists.txt") # `touch` to force gradle to refresh cmake project
    facewolf.inject_env()
    gradle = 'gradlew.bat' if os_windows() else './gradlew'
    buildcommand = ':facewolf:assembleRelease'
    if args.clean: buildcommand = ':facewolf:clean ' + buildcommand
    execute(f"cd AndroidStudio && {gradle} {buildcommand}")
    for fl in glob.glob("android/*.aar"):
        os.remove(fl)
    build_number = os.getenv('BUILD_NUMBER')
    aar_name = f'facewolf-{build_number}.aar' if build_number else 'facewolf.aar'
    for fl in glob.glob("*.aar"):
        os.remove(fl)
    shutil.move("AndroidStudio/facewolf/build/outputs/aar/facewolf-release.aar", aar_name)

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
        if os_windows():
            proc.send_signal(subprocess.signal.CTRL_C_EVENT)
        else:
            proc.send_signal(subprocess.signal.SIGINT)
        raise
    if proc.returncode == 0:
        return
    raise CalledProcessError(proc.returncode, ' '.join(args))


######################################################################################

def facewolf_setup():
    if not args.ios and not args.android and not args.linux and not args.mac and not args.windows:
        raise Exception('Expected platform argument such as --windows or --android !')

