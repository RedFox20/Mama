import mama
import os

class test(mama.BuildTarget):
    workspace = 'packages'

    def build(self):
        self.nothing_to_build()

    def dependencies(self):
        url = os.environ['MAMA_TEST_REMOTE_URL']  # the local bare repo the example_remote fixture built
        old = os.environ['MAMA_TEST_REMOTE_OLD']  # commit and tag v1.0.0: no REMOTE_VERSION
        new = os.environ['MAMA_TEST_REMOTE_NEW']  # commit and tag v2.0.0: REMOTE_VERSION 2
        self.add_git('ExampleRemote', url, git_tag='v1.0.0')
        self.add_git('ExampleRemote2', url, git_tag='v2.0.0')
        self.add_git('ExampleRemote3', url, git_tag=old)
        self.add_git('ExampleRemote4', url, git_tag=new)
