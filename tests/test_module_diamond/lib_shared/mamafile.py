import mama


class LibShared(mama.BuildTarget):
    def settings(self):
        self.enable_cxx20()

    def package(self):
        # names no module: the automatic export finds shared-api.cppm under the exported include dir
        self.export_include('src', build_dir=False)
        self.export_lib('libLibShared.a' if not self.windows else f'{self.cmake_build_type}/LibShared.lib')
