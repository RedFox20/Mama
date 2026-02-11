import mama

class test(mama.BuildTarget):
    workspace = 'packages'

    def build(self):
        self.nothing_to_build()

    def dependencies(self):
        self.add_git('ExampleRemote', 'https://github.com/BatteredBunny/MamaExampleRemote.git')
