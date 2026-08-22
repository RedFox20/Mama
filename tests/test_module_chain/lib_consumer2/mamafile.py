import mama


class LibConsumer2(mama.BuildTarget):
    def settings(self):
        self.enable_cxx20()

    def dependencies(self):
        self.add_local('LibConsumer1', '../lib_consumer1')

    def package(self):
        self.export_include('src', build_dir=False)
        self.export_lib('libLibConsumer2.a' if not self.windows else f'{self.cmake_build_type}/LibConsumer2.lib')
