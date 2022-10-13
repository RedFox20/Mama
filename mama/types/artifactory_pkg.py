from .dep_source import DepSource

class ArtifactoryPkg(DepSource):
    """
    For BuildDependency whose source is from an Artifactory Package
    """
    def __init__(self, name:str, version:str, fullname:str):
        super(ArtifactoryPkg, self).__init__(name)
        self.is_pkg = True
        if fullname:
            self.fullname = fullname
            self.version = ''
        else:
            self.fullname = ''
            self.version = version


    def __str__(self):  return f'DepSource ArtifactoryPkg {self.name} {self.fullname if self.fullname else self.version}'
    def __repr__(self): return self.__str__()


    @staticmethod
    def from_papa_string(s: str) -> "ArtifactoryPkg":
        p = s.split(',')
        name, version, fullname = p[0:3]
        return ArtifactoryPkg(name, version, fullname)


    def get_papa_string(self):
        fields = DepSource.papa_join(
            self.name, self.fullname, self.version)
        return 'pkg ' + fields
