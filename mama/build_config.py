import os, sys, multiprocessing, subprocess
from mama.system import System, console


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
        self.configure = False # re-run cmake configure
        self.reclone   = False
        self.test      = None
        self.windows = False
        self.linux   = False
        self.macos   = False
        self.ios     = False
        self.android = False
        self.linux_clang = True # prefer clang on linux
        self.linux_gcc   = False   
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
        self.global_workspace = True
        self.workspaces_root = os.getenv('HOMEPATH') if System.windows else os.getenv('HOME')
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
            self.set_platform(windows=System.windows, linux=System.linux, macos=System.macos)
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
        elif arg == 'update':    self.update  = True
        elif arg == 'configure': self.configure = True
        elif arg == 'reclone':   self.reclone   = True
        elif arg == 'test':      self.test      = True
        elif arg == 'windows': self.set_platform(windows=True)
        elif arg == 'linux':   self.set_platform(linux=True)
        elif arg == 'macos':   self.set_platform(macos=True)
        elif arg == 'ios':     self.set_platform(ios=True)
        elif arg == 'android': self.set_platform(android=True)
        elif arg == 'clang':   self.linux_gcc = False
        elif arg == 'gcc':     self.linux_gcc = True
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
                #console(f'Found Ninja Build System: {ninja_exe}')
                return ninja_exe
        return ''
    def init_ndk_path(self):
        androidenv = os.getenv('ANDROID_HOME')
        paths = [androidenv] if androidenv else []
        if System.windows: paths += [f'{os.getenv("LOCALAPPDATA")}\\Android\\Sdk']
        elif System.linux: paths += ['/usr/bin/android-sdk', '/opt/android-sdk']
        elif System.macos: paths += [f'{os.getenv("HOME")}/Library/Android/sdk']
        ext = '.cmd' if System.windows else ''
        for sdk_path in paths:
            if os.path.exists(f'{sdk_path}/ndk-bundle/ndk-build{ext}'):
                self.android_sdk_path = sdk_path
                self.android_ndk_path = sdk_path  + '/ndk-bundle'
                #console(f'Found Android NDK: {self.ndk_path}')
                return
        return ''
    def libname(self, library):
        if self.windows: return f'{library}.lib'
        else:            return f'lib{library}.a'
    def libext(self):
        return 'lib' if self.windows else 'a'
    def target_matches(self, target_name):
        return self.target == 'all' or self.target == target_name