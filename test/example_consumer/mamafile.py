import mama

class ExampleConsumer(mama.BuildTarget):
    workspace = 'wolf3d'
    def dependencies(self):
        self.add_local('ExampleLibrary', '../example_library')
        self.add_git('ExampleRemote', 'https://github.com/RedFox20/MamaExampleRemote.git')
