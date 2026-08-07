import os
from .dep_source import DepSource
from ..utils.fileio import save_file_if_contents_changed, read_text_from
from ..utils.git_status import (git_dir_fingerprint, source_walk_moved, record_source_walk,
                                git_source_changed)
from ..utils.paths import path_join

class LocalSource(DepSource):
    """For a BuildDependency whose source is a local directory."""
    def __init__(self, name:str, rel_path:str, mamafile:str, always_build:bool, args:list,
                 version_suffix:str=''):
        super(LocalSource, self).__init__(name)
        self.is_src = True
        self.rel_path = rel_path
        self.mamafile = mamafile
        self.always_build = always_build
        self.args = args
        self.version_suffix = version_suffix

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
        """True when a build input in the subfolder differs from the snapshot stored at the last build."""
        f = self.src_status_file(dep)
        stored = read_text_from(f) if os.path.exists(f) else ''
        if not source_walk_moved(dep.src_dir, dep.build_dir): return False  # the cheap gate, Windows only
        unchanged = not git_source_changed(dep.src_dir) or \
                    self.working_tree_fingerprint(dep, 'did the subfolder change since the last build') == stored
        if unchanged:
            record_source_walk(dep.src_dir, dep.build_dir)  # proven unchanged, so arm the gate now
        return not unchanged

    def save_status(self, dep):
        save_file_if_contents_changed(self.src_status_file(dep),
                                      self.working_tree_fingerprint(dep, 'record the tree this build used'))
        record_source_walk(dep.src_dir, dep.build_dir)

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
