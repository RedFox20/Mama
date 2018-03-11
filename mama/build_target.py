import urllib.request, ssl, os.path, shutil, zipfile
import pathlib, stat, time, subprocess, concurrent.futures
from mama.system import System, console
from mama.util import execute, save_file_if_contents_changed, glob_with_extensions, normalized_path
from mama.build_dependency import BuildDependency, Git

######################################################################################

class BuildTarget:
    def __init__(self, name, config, dep):
        if config is None: raise RuntimeError('BuildTarget config argument must be set')
        if dep is None:    raise RuntimeError('BuildTarget dep argument must be set')
        self.config = config
        self.name = name
        self.dep = dep
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
        self.exported_includes = [] # include folders to export from this target
        self.exported_libs     = [] # libs to export from this target

    ###
    # Add a local dependency
    def add_local(self, name, source_dir):
        src = source_dir
        if not os.path.isabs(src):
            src = os.path.join(self.dep.src_dir, src)
            src = os.path.abspath(src)
        dependency = BuildDependency(name, self.config, BuildTarget, workspace=self.dep.workspace, src=src)
        self.dep.children.append(dependency)

    ###
    # Add a remote dependency
    def add_git(self, name, git_url, git_branch='', git_tag=''):
        git = Git(git_url, git_branch, git_tag)
        dependency = BuildDependency(name, self.config, BuildTarget, workspace=self.dep.workspace, git=git)
        self.dep.children.append(dependency)

    ## Sets a build dependency to prevent unnecessary rebuilds
    def set_build_dependency(self, all='', windows='', android='', ios='', linux='', mac=''):
        dependency = all if all else self.select(windows, android, ios, linux, mac)
        if dependency:
            self.build_dependency = normalized_path(os.path.join(self.dep.build_dir, dependency))
            #console(f'    {self.name}.build_dependency = {self.build_dependency}')

    def add_export_include(self, include):
        include = os.path.join(self.dep.src_dir, include)
        self.exported_includes.append(normalized_path(include))

    ## Export includes relative to source directory
    def export_includes(self, includes=['']):
        self.exported_includes = []
        for include in includes: self.add_export_include(include)

    def add_export_lib(self, relative_path):
        path = os.path.join(self.dep.build_dir, relative_path)
        self.exported_libs.append(normalized_path(path))

    ## Export libs relative to build directory
    def export_libs(self, path = '.', extensions = ['.lib', '.a', '.dll', '.so']):
        path = os.path.join(self.dep.build_dir, path)
        self.exported_libs = glob_with_extensions(path, extensions)


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
            if self.config.windows:     return f'/v:m {self.mp_flags()} /nologo'
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
            if System.windows:
                return f'{self.config.ndk_path}\\prebuilt\\windows-x86_64\\bin\\make.exe' # CodeBlocks - Unix Makefiles
            elif System.macos:
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

        opt = ["CMAKE_POSITION_INDEPENDENT_CODE=ON"]
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


    def copy_built_file(self, builtFile, copyToFolder):
        src = f'{self.dep.build_dir}/{builtFile}'
        dst = f'{self.dep.build_dir}/{copyToFolder}'
        shutil.copy(src, dst)

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

    ########## Customization Points ###########

    ###
    # Add any dependencies in this step
    #   self.add_local(...)
    #   self.add_remote(...)
    def dependencies(self):
        pass

    ###
    # Perform any pre-build steps here
    def build(self):
        pass

    ###
    # Perform any post-build steps to package the products
    def package(self):
        pass

    ############################################

    def prepare_install_target(self, install):
        if not self.install_target or not install:
            return ''
        return f'--target {self.install_target}'

    def run_cmake(self, cmake_command):
        cmd = f"cd {self.dep.build_dir} && cmake {cmake_command}"
        #console(cmd)
        execute(cmd)

    def install(self):
        console('\n\n#############################################################')
        console(f"CMake install {self.name} ...")
        self.run_cmake(f"--build . --config {self.cmake_build_type} {self.prepare_install_target(True)}")

    def clean(self):
        self.dep.clean()

    ## Build only this target and nothing else
    def build_target(self):
        if self.dep.should_rebuild:
            console('\n\n#############################################################')
            console(f"CMake build {self.name} ...")

            flags = self.get_cmake_flags()
            gen = self.cmake_generator()
            self.run_cmake(f'{gen} {flags} -DCMAKE_INSTALL_PREFIX="." . "{self.dep.src_dir}"')

            self.inject_env()
            self.run_cmake(f"--build . --config {self.cmake_build_type} {self.prepare_install_target(True)} {self.buildsys_flags()}")
            self.dep.save_git_commit()
        self.package()

######################################################################################


        