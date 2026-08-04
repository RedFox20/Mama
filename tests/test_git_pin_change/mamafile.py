import mama
import os

class test(mama.BuildTarget):
    workspace = 'packages'

    def build(self):
        self.nothing_to_build()

    def dependencies(self):
        stage = os.environ.get('GIT_PIN_CHANGE_TEST') # the env variable selects the pin per stage
        name = 'ExampleRemote'
        url = os.environ['MAMA_TEST_REMOTE_URL']  # the local bare repo the example_remote fixture built
        old = os.environ['MAMA_TEST_REMOTE_OLD']  # commit and tag v1.0.0: no REMOTE_VERSION
        new = os.environ['MAMA_TEST_REMOTE_NEW']  # commit and tag v2.0.0: REMOTE_VERSION 2

        if   stage == '0': self.add_git(name, url, git_commit=old)
        elif stage == '1': self.add_git(name, url, git_commit=new)
        elif stage == '2': self.add_git(name, url, git_tag='v1.0.0')
        elif stage == '3': self.add_git(name, url, git_tag='v2.0.0')
        elif stage == '4': self.add_git(name, url, git_branch='old')     # branched from the old commit
        elif stage == '5': self.add_git(name, url, git_branch='master')  # the latest commit
