import mama
import os

class test(mama.BuildTarget):
    workspace = 'packages'

    def build(self):
        self.nothing_to_build()

    def dependencies(self):
        stage = os.environ.get('GIT_PIN_CHANGE_TEST') # Uses env variable to dynamically change the pinned commit

        remote_name = 'ExampleRemote'
        remote_url = 'https://github.com/BatteredBunny/MamaExampleRemote.git'

        # Switches between having REMOTE_VERSION and not to demonstrate that the contents actually change
        if stage == '0':
            self.add_git(remote_name, remote_url, git_commit='4acd9052f27a459314651dd485ae8fa79a04d49d') # has no REMOTE_VERSION
        if stage == '1':
            self.add_git(remote_name, remote_url, git_commit='993e326cf840bc2df9d67b14d6e2fe0d38736713') # has REMOTE_VERSION 2
        elif stage == '2':
            self.add_git(remote_name, remote_url, git_tag='v1.0.0') # has no REMOTE_VERSION
        elif stage == '3':
            self.add_git(remote_name, remote_url, git_tag='v2.0.0') # has REMOTE_VERSION 2