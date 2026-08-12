import mama, os


class Producer(mama.BuildTarget):

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

    def configure(self):
        self.enable_cxx20()
        if os.getenv('MAMA_TEST_MODULES') == '1': self.add_cmake_options('BUILD_WITH_MODULES=ON')

    def package(self):
        self.export_include('src/rpp', includes_filter=['.h'], as_includes_root='rpp')
        self.export_modules('src/rpp', ['rpp-strview.cppm'])
        self.export_lib('libProducer.a')
