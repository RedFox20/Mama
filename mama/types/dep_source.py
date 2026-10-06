
class DepSource(object):
    # A parent declares this on add_git/add_local/add_artifactory_pkg. It appends to the archive version,
    # so a changed packaging recipe renames the package on every platform and compiler at once.
    version_suffix = ''

    def __init__(self, name:str):
        self.name = name
        self.is_git = False
        self.is_pkg = False
        self.is_src = False


    def get_type_string(self):
        if self.is_git: return "Git"
        if self.is_pkg: return "ART"
        if self.is_src: return "Src"


    def get_papa_string(self):
        raise RuntimeError('get_papa_string() not implemented')


    def artifacts_behind_source(self, dep) -> bool:
        """True when the artifacts of `dep` came from another source than its source dir holds now.
        A package has no source dir, so its artifacts always match."""
        return False


    @staticmethod
    def papa_join(*fields):
        """ Join all given fields for PAPA package serialization """
        strings = []
        for field in fields:
            if field:
                if isinstance(field, list):
                    strings += field
                else:
                    strings.append(field)
            else:
                strings.append('')
        return ",".join(strings)
