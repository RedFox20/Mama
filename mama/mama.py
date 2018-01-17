#!/usr/bin/python3.6
import urllib.request, ssl, os.path, shutil, platform, glob, sys, zipfile
import ctypes, traceback, argparse, pathlib, random
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

def init_ninja_path():
    if os_macos() or os_linux():
        return run(['which', 'ninja'], stdout=PIPE).stdout.decode('utf-8').strip()
    ninjaenv = os.getenv('NINJA')
    ninjapaths = [ninjaenv] if ninjaenv else []
    ninjapaths += ['/Projects/ninja.exe']
    for ninja in ninjapaths:        
        if os.path.exists(ninja):
            console(f'Found Ninja Build System: {ninja}')
            return ninja
    return ''
ninja_path = init_ninja_path()


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
        self.cmake_ndk_toolchain = f'{ndk_path}/build/cmake/android.toolchain.cmake'
        self.cmake_ios_toolchain = ''
        self.cmake_opts     = []
        self.cmake_cxxflags = ''
        self.cmake_ldflags  = ''
        self.cmake_build_type = 'Debug' if args.debug else 'RelWithDebInfo'
        self.enable_exceptions = True
        self.enable_unix_make = False
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
        if ninja_path:
            return ninja_path
        ninja_executables = [
            os.getenv('NINJA'), 
            self.find_executable_from_system('ninja'),
            '/Projects/ninja'
        ]
        for ninja_exe in ninja_executables:        
            if ninja_exe and os.path.isfile(ninja_exe):
                console(f'Found Ninja Build System: {ninja_exe}')
                ninja_path = ninja_exe
                return ninja_exe
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
        if self.enable_ninja_build: return ninja_path
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
            self.console(f"Using locally cached {local_file}")
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

def install_opencv():
    def options():
        opt = [
            "ENABLE_OMIT_FRAME_POINTER=ON", "ENABLE_PRECOMPILED_HEADERS=ON", "ENABLE_CCACHE=ON",
            "BUILD_DOCS=OFF",  "BUILD_EXAMPLES=OFF", "BUILD_TESTS=OFF", "BUILD_PERF_TESTS=OFF",
            "WITH_OPENGL=ON",  "WITH_IPP=OFF",    "WITH_OPENCL=OFF", "WITH_1394=OFF",    "WITH_CUDA=OFF",
            "WITH_OPENGL=ON",  "WITH_JASPER=OFF", "WITH_WEBP=OFF",   "WITH_OPENEXR=OFF", "WITH_TIFF=OFF", "WITH_FFMPEG=OFF",
            "BUILD_OPENEXR=OFF", "BUILD_TIFF=OFF", "BUILD_JPEG=ON",
            "BUILD_PNG=ON",      "BUILD_ZLIB=ON",  "BUILD_JASPER=OFF",
            "BUILD_opencv_apps=OFF",      "BUILD_opencv_calib3d=ON",   "BUILD_opencv_core=ON",
            "BUILD_opencv_features2d=ON", "BUILD_opencv_flann=ON",     "BUILD_opencv_highgui=ON",
            "BUILD_opencv_imgcodecs=ON",  "BUILD_opencv_imgproc=ON",   "BUILD_opencv_ml=ON",
            "BUILD_opencv_objdetect=ON",  "BUILD_opencv_photo=OFF",    "BUILD_opencv_shape=OFF",
            "BUILD_opencv_stitching=OFF", "BUILD_opencv_superres=OFF", "BUILD_opencv_ts=OFF",
            "BUILD_opencv_video=ON",      "BUILD_opencv_videoio=ON",   "BUILD_opencv_videostab=OFF",
            "BUILD_opencv_nonfree=OFF", "BUILD_SHARED_LIBS=OFF", "BUILD_opencv_java=OFF", 
            "BUILD_opencv_python2=OFF", "BUILD_opencv_python3=OFF"
        ]
        if   args.android: opt += ['BUILD_ANDROID_EXAMPLES=OFF', 'BUILD_opencv_androidcamera=ON']
        elif args.ios:     opt += ['IOS_ARCH=arm64']
        elif args.windows: opt += ['BUILD_WITH_STATIC_CRT=OFF']
        elif args.linux:   opt += []
        return opt

    opencv = cmake_builder("OpenCV 3.3.1 fork", "StructureFromMotion/3rdparty/opencv", 
                           "https://github.com/wolfprint3d/opencv.git")
    opencv.cmake_build_type = 'Release'
    cvcore = libname('opencv_core', '331')
    opencv.set_dependency(windows=f"x64/vc15/staticlib/{cvcore}",
                          android=f'sdk/native/libs/{opencv.android_arch}/{cvcore}',
                          linux=f'lib/{cvcore}',
                          ios=f'lib/{cvcore}',
                          mac=f'lib/{cvcore}')
    opencv.cmake_ios_toolchain = '../platforms/ios/cmake/Toolchains/Toolchain-iPhoneOS_Xcode.cmake'
    if args.ios:
        opencv.enable_ninja_build = False # opencv for ios blows up with Ninja
    opencv.add_cmake_options(options())
    opencv.clone_build_install()


def install_eigen():
    eigen = cmake_builder("Eigen 3.2", "StructureFromMotion/3rdparty/eigen", "https://github.com/wolfprint3d/eigen.git", branch='branches/3.2')
    eigen.clone()


def install_ceres():
    ceres = cmake_builder('Ceres', 'StructureFromMotion/3rdparty/ceres', 'https://github.com/wolfprint3d/ceres-solver.git')
    ceres.set_dependency(all=f"lib/{libname('ceres')}")
    ceres.add_cmake_options('EIGEN_INCLUDE_DIR=../../eigen',
                            'BUILD_TESTING=OFF',
                            'BUILD_EXAMPLES=OFF',
                            'MINIGLOG=ON',
                            'EIGENSPARSE=ON',
                            'CXX11=OFF',
                            'MAX_LOG_LEVEL=-1',
                            'MINIGLOG_MAX_LOG_LEVEL=-1')
    ceres.cmake_ios_toolchain = '../cmake/iOS.cmake'
    ceres.enable_cxx14()
    ceres.enable_exceptions = False
    ceres.add_cxx_flags(clang='-g0') # -g0: no debug symbols
    ceres.add_linker_flags(android='-s') # -s: strip all symbols
    ceres.clone_build_install()


def install_c_ares():
    build_c_ares = args.android
    if not build_c_ares:
        return

    c_ares = cmake_builder('C-Ares', '3rdparty/cares', "https://github.com/c-ares/c-ares.git")
    c_ares.set_dependency(all=f"lib/{libname('cares')}")
    c_ares.add_cmake_options('CARES_STATIC=ON', 'CARES_SHARED=OFF')
    c_ares.clone_build_install()


def install_curl():
    build_curl = args.windows or args.ios or args.mac or args.linux
    if not build_curl:
        return
    
    curl = cmake_builder('CURL', '3rdparty/curl', 'https://github.com/wolfprint3d/curl.git')
    
    curl.set_dependency(all=f"lib/libcurl.{libext}")
    curl.add_cmake_options('CURL_STATICLIB=ON', 'BUILD_TESTING=OFF', 'BUILD_CURL_EXE=OFF',
                           'CURL_ZLIB=OFF', 'CURL_DISABLE_LDAP=ON', 'CURL_DISABLE_LDAPS=ON', 
                           'CURL_HIDDEN_SYMBOLS=ON', 'ENABLE_MANUAL=OFF')
    if args.windows:
        openssl = f'{cwd}/3rdparty/openssl'
        curl.add_cmake_options('CMAKE_USE_OPENSSL=ON', 'CMAKE_USE_LIBSSH2=OFF', 
                               f'OPENSSL_LIBRARIES="{openssl}/libsslMD.lib;{openssl}/libcryptoMD.lib"',
                               f'OPENSSL_INCLUDE_DIR={openssl}')
    elif args.ios or args.mac:
        curl.add_cmake_options('CMAKE_USE_DARWINSSL=ON', 'CURL_CA_PATH=none', 'CMAKE_USE_LIBSSH2=OFF', 
                               'HAVE_FSETXATTR=OFF', 'HAVE_STRERROR_R=ON')
        if args.ios: curl.add_cmake_options('CURL_BUILD_IOS=ON')

    curl.clone_build_install()


def install_aws_sdk():
    aws = cmake_builder('AWS C++ SDK', '3rdparty/awssdk', 'https://github.com/wolfprint3d/aws-sdk-cpp.git')
    aws.set_dependency(all=f"lib/{libname('aws-cpp-sdk-core')}")

    # https://github.com/aws/aws-sdk-cpp/blob/master/README.md
    aws.add_cmake_options('BUILD_ONLY="s3;core;transfer;cognito-identity;identity-management"',
                          'BUILD_SHARED_LIBS=OFF',
                          'SIMPLE_INSTALL=ON',
                          'DISABLE_ANDROID_STANDALONE_BUILD=ON',
                          'CPP_STANDARD=14',
                          'ENABLE_TESTING=OFF')
    #aws.cmake_ios_toolchain = '../../../cmake/iOS.cmake'
    # if args.ios:
    #     aws.add_linker_flags(ios=' -framework Foundation -lz -framework Security')

    if args.android:
        c_ares = f'{cwd}/3rdparty/cares/{aws.build_name()}'
        aws.add_cmake_options([f'CARES_LIBRARY="{c_ares}/lib/libcares.a"', f'CARES_INCLUDE="{c_ares}/include"'])

    if args.ios:
        curl = f'{cwd}/3rdparty/curl'
        aws.add_cmake_options(f'CURL_INCLUDE_DIRS={curl}/ios/include',
                              f'CURL_LIBRARIES={curl}/ios/lib/libcurl.a')

    aws.clone_build_install()


def install_libzip():
    libzip = cmake_builder('libzip', '3rdparty/libzip', 'https://github.com/wolfprint3d/libzip-1.3.0.git')
    libzip.set_dependency(all=f"lib/{libname('zip')}")

    opencv = f'{cwd}/StructureFromMotion/3rdparty/opencv'
    libzip.add_platform_options(windows=[f'ZLIB_LIBRARY={opencv}/windows/staticlib/zlib.lib',
                                         f'ZLIB_INCLUDE_DIR=../zlib/windows'])
    libzip.clone_build_install()


def install_glfw():
    if args.android or args.ios:
        return # GLFW is only for Desktop

    glfw = cmake_builder('GLFW', 'StructureFromMotion/3rdparty/glfw', 'https://github.com/glfw/glfw.git')
    glfw.set_dependency(all=f"lib/{libname('glfw3')}")
    glfw.clone_build_install()


def install_openssl():
    if args.windows:
        copy_files('3rdparty/openssl', 'bin', ['libcryptoMD.dll','libcryptoMD.pdb','libsslMD.dll','libsslMD.pdb'])


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

def gather_facewolf_products(facewolf):
    if args.windows:
        copy_files(f'bin/{facewolf.cmake_build_type}', 'bin', [
            'FaceWolf.dll', 'FaceWolf.pdb', 'Tests.exe', 'Tests.pdb'
        ])
    # if args.linux:
    #     copy_files('linux', 'bin', ['libFaceWolf.so'])
    if args.mac:
        framework = f'bin/{facewolf.cmake_build_type}/FaceWolf.bundle'
        execute("rm -rf FaceWolf.bundle")
        deploy_framework(framework, './')
        deploy_framework(framework, '../Fable/Assets/FaceWolf/Plugins/')
    if args.ios:
        framework = f'ios/{facewolf.cmake_build_type}-iphoneos/FaceWolf.framework'
        execute("rm -rf FaceWolf.framework")
        deploy_framework(framework, './')
        deploy_framework(framework, '../Fable/ios/Frameworks/FaceWolf/Plugins/')
        deploy_framework(framework, '../Fable/Assets/FaceWolf/Plugins/')

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

def build_facewolf():
    install_openssl()

    facewolf = cmake_builder('FaceWolf', './')
    facewolf.cmake_ios_toolchain = 'cmake/iOS.cmake'
    facewolf.enable_ninja_build = not (args.ios or args.mac) # Allow it to generate Xcode project

    if args.clean:
        facewolf.clean()

    if args.android:
        build_facewolf_android(facewolf)
        return

    facewolf.build(install=False)
    gather_facewolf_products(facewolf)

    if args.tests != None:
        testargs = ' '.join(args.tests)
        testcommand = ''
        if args.windows: testcommand = f".\\Tests.exe"
        elif args.linux: testcommand = f"./Tests"
        elif args.mac:   testcommand = f"./{facewolf.cmake_build_type}/Tests.app/Contents/MacOS/Tests"
        if testcommand:
            console(f'run {testcommand} {testargs}')
            run_with_timeout(testcommand, testargs, "bin", timeoutSeconds=60.0)


######################################################################################

def facewolf_setup():
    if not args.ios and not args.android and not args.linux and not args.mac and not args.windows:
        raise Exception('Expected platform argument such as --windows or --android !')

