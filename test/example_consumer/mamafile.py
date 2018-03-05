import mama

class ExampleConsumer(mama.BuildTarget):

    def configure(self):
        print("ExampleConsumer.configure")
        self.workspace = 'wolf3d'
        self.add_local('ExampleLibrary', '../example_library')

    def build(self):
        print("ExampleConsumer.build")

    def package(self):
        print("ExampleConsumer.package")






