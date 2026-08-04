import mama
import os

class ExampleConsumer(mama.BuildTarget):
    workspace = 'packages'

    def dependencies(self):
        self.add_git('ExampleRemote', os.environ['MAMA_TEST_REMOTE_URL'],
                     git_tag=os.environ['MAMA_TEST_REMOTE_OLD'])
