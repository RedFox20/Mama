import mama
import os

class test(mama.BuildTarget):
    workspace = 'packages'

    def build(self):
        self.nothing_to_build()

    def dependencies(self):
        self.add_git('ExampleRemote', os.environ['MAMA_TEST_REMOTE_URL'])
