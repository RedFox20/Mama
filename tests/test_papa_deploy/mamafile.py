import mama

class ExampleConsumer(mama.BuildTarget):
    workspace = 'packages'

    def dependencies(self):
        self.add_git('ExampleRemote', 'https://github.com/BatteredBunny/MamaExampleRemote.git', git_tag='4acd9052f27a459314651dd485ae8fa79a04d49d')
