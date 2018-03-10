import mama

class ExampleConsumer(mama.BuildTarget):
    workspace = 'wolf3d'
    def configure(self):
        print("ExampleConsumer.configure")
        self.add_local('ExampleLibrary', '../example_library')

    def build(self):
        print("ExampleConsumer.build")

    def package(self):
        print("ExampleConsumer.package")


print('executed example_consumer/mamafile.py')




