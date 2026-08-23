import mama, os


class LibConsumer1(mama.BuildTarget):
    def settings(self):
        self.enable_cxx20()

    def dependencies(self):
        self.add_git('ReCpp', 'https://github.com/RedFox20/ReCpp.git',
                     git_branch=os.getenv('RECPP_BRANCH', 'master'))

    def package(self):
        # names no module: the automatic export finds lib1-api.cppm under the exported include dir
        self.export_include('src', build_dir=False)
        self.export_lib('libLibConsumer1.a' if not self.windows else f'{self.cmake_build_type}/LibConsumer1.lib')
