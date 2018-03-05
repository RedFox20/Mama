import mama

class ExampleLibrary(mama.BuildTarget):
    
    def configure(self):
        print("ExampleLibrary.configure")

    def build(self):
        print("ExampleLibrary.build")

    def package(self):
        print("ExampleLibrary.package")






