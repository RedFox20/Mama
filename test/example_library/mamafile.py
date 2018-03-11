import mama

class ExampleLibrary(mama.BuildTarget):
    
    def dependencies(self):
        self.set_build_dependency("bin/ExampleLibrary.lib")

    def build(self):
        pass

    def package(self):
        self.export_includes(".")
        self.export_libs("bin")
