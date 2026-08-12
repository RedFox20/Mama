import mama, os


class Consumer(mama.BuildTarget):
    workspace = 'packages'

    def settings(self):
        self._pin_compiler()

    def _pin_compiler(self):
        """Point mama at the exact compiler this test resolved, and pick the generator."""
        if os.getenv('MAMA_TEST_NO_NINJA') == '1': self.disable_ninja_build()
        cxx = os.getenv('MAMA_TEST_CXX')
        if not cxx: return
        if os.getenv('MAMA_TEST_MODULE_COMPILER') == 'clang':
            self.prefer_clang()
            self.config.use_gcc_stdlib_for_clang() # a CI image often ships no libc++
        self.config.cc_path = os.getenv('MAMA_TEST_CC')
        self.config.cxx_path = cxx
        self.config.cxx_version = os.getenv('MAMA_TEST_CXX_VERSION')

    def dependencies(self):
        self.add_local('Producer', 'producer')

    def configure(self):
        self.enable_cxx20()
        # The shipped floor answers for any package. This fixture is a thin facade, so it builds on
        # the older clang this host may carry.
        self.add_cmake_options(f'MAMA_MODULES_MIN_CLANG={os.getenv("MAMA_TEST_MIN_CLANG", "21")}')
        if os.getenv('MAMA_TEST_WHOLE_ARCHIVE') == '1': self.add_cmake_options('WHOLE_ARCHIVE_LINK=ON')
