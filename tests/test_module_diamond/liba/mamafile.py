import mama


class LibA(mama.BuildTarget):
    def settings(self):
        self.enable_cxx20()

    def dependencies(self):
        self.add_local('LibShared', '../lib_shared')

    def package(self):
        self.export_include('src', build_dir=False)
        self.export_lib('libLibA.a' if not self.windows else f'{self.cmake_build_type}/LibA.lib')
