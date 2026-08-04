import os
from .dep_source import DepSource
from ..util import git_dir_fingerprint, path_join, save_file_if_contents_changed, read_text_from

class LocalSource(DepSource):
    """For a BuildDependency whose source is a local directory."""
    def __init__(self, name:str, rel_path:str, mamafile:str, always_build:bool, args:list):
        super(LocalSource, self).__init__(name)
        self.is_src = True
        self.rel_path = rel_path
        self.mamafile = mamafile
        self.always_build = always_build
        self.args = args

    def __str__(self):
        return f'DepSource LocalSource {self.name} {self.rel_path} {self.mamafile} always_build={self.always_build}'
    def __repr__(self): return self.__str__()

    # A local dep has no git_status of its own. The working-tree state of the enclosing repo gates
    # its cmake step. The snapshot lives in the build dir, next to git's git_status.
    def src_status_file(self, dep) -> str:
        return path_join(dep.build_dir, 'src_status')

    def working_tree_fingerprint(self, dep, reason='') -> str:
        """Fingerprint of uncommitted edits in this dep's subfolder, as tracked by an enclosing git
        repo. '' when the subfolder is clean or not under git. See git_dir_fingerprint.

        A local module lives inside the root working tree, so the run's shared status already knows
        whether this subfolder changed. That answer costs no process."""
        return git_dir_fingerprint(dep.src_dir, shared_status=True, reason=f'local {reason}')

    def source_tree_changed(self, dep) -> bool:
        """True when the subfolder differs from the snapshot stored at the last build."""
        f = self.src_status_file(dep)
        stored = read_text_from(f) if os.path.exists(f) else ''
        return self.working_tree_fingerprint(dep, 'did the subfolder change since the last build') != stored

    def save_status(self, dep):
        save_file_if_contents_changed(self.src_status_file(dep),
                                      self.working_tree_fingerprint(dep, 'record the tree this build used'))

    @staticmethod
    def from_papa_string(s: str) -> "LocalSource":
        p = s.split(',')
        name, rel_path, mamafile, always_build = p[0:4]
        args = p[4:]
        return LocalSource(name, rel_path, mamafile, bool(always_build), args)


    def get_papa_string(self):
        fields = DepSource.papa_join(
            self.name, self.rel_path, self.mamafile, self.always_build, self.args
        )
        return 'src ' + fields
