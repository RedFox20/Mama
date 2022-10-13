from .dependency_source import DependencySource

class ArtifactoryPackage(DependencySource):
    """
    For BuildDependency whose source is from an Artifactory Package
    """
    def __init__(self, name:str, version:str, fullname:str):
        super(ArtifactoryPackage, self).__init__(name, is_pkg=True)
        self.is_pkg = True
        if self.fullname:
            self.fullname = fullname
            self.version = ''
        else:
            self.fullname = ''
            self.version = version


    def __str__(self):  return f'pkg {self.name} {self.fullname if self.fullname else self.version}'
    def __repr__(self): return self.__str__()


    @staticmethod
    def from_papa_string(s: str) -> "ArtifactoryPackage":
        p = s.split(',')
        name, version, fullname = p
        return ArtifactoryPackage(name, version, fullname)


    def get_papa_string(self):
        fields = DependencySource.papa_join(
            self.name, self.fullname, self.version)
        return 'pkg ' + fields
