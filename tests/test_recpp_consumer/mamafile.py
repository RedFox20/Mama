import mama, os

class consumer(mama.BuildTarget):
    """Clones a real C++ package, builds it, and imports its C++20 module."""

    def dependencies(self):
        # ReCpp ships its own mamafile, so mama clones it and reads the packaging rules from there
        self.add_git('ReCpp', 'https://github.com/RedFox20/ReCpp.git',
                     git_branch=os.getenv('RECPP_BRANCH', 'master'))

    def settings(self):
        if os.getenv('USE_GCC_STDLIB'): self.config.use_gcc_stdlib_for_clang()
        if os.getenv('NO_MODULES'): self.disable_ninja_build()  # forces the header fallback

    def configure(self):
        self.enable_cxx20()
        # the lever a consumer turns when a package ships modules its toolchain cannot compile
        if os.getenv('NO_MAMA_MODULES'): self.add_cmake_options('MAMA_ENABLE_MODULES=OFF')
