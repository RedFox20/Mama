import mama

class ExampleConsumer(mama.BuildTarget):
    workspace = 'wolf3d'
    def configure(self):
        self.add_local('ExampleLibrary', '../example_library')

    def build(self):
        pass

    def package(self):
        pass





