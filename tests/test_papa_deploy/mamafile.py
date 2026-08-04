import mama
import os

class ExampleConsumer(mama.BuildTarget):
    workspace = 'packages'

    def dependencies(self):
        # the buildable remote: this consumer links the library, so the clone must really build
        self.add_git('ExampleRemote', os.environ['MAMA_TEST_BUILD_REMOTE_URL'],
                     git_tag=os.environ['MAMA_TEST_BUILD_REMOTE_OLD'])
