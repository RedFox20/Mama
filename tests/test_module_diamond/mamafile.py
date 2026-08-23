import mama, os


class TopConsumer(mama.BuildTarget):
    workspace = 'packages'

    def settings(self):
        self.enable_cxx20()
        if os.getenv('MAMA_TEST_MODULE_COMPILER') == 'clang':
            self.prefer_clang()
            self.config.use_gcc_stdlib_for_clang()  # a CI image often ships no libc++
        cxx = os.getenv('MAMA_TEST_CXX')
        if not cxx: return
        self.config.cc_path = os.getenv('MAMA_TEST_CC')
        self.config.cxx_path = cxx
        self.config.cxx_version = os.getenv('MAMA_TEST_CXX_VERSION')

    def dependencies(self):
        self.add_local('LibA', 'liba')
        self.add_local('LibB', 'libb')
