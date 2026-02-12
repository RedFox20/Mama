import mama

class ExampleConsumer(mama.BuildTarget):
    workspace = 'packages'

    def init(self):
        self.prefer_gcc()

    def dependencies(self):
        self.add_local('ExampleLibrary', '../example_library')
        self.add_git('ExampleRemote', 'https://github.com/RedFox20/MamaExampleRemote.git',
                     git_tag='4acd9052f27a459314651dd485ae8fa79a04d49d')

    # optional: pre-build configuration step
    def configure(self):
        pass
    
    # optional: post-build package step
    def package(self):
        pass

