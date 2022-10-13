from .dep_source import DepSource

class LocalSource(DepSource):
    """
    For BuildDependency whose source is from a Local Source
    """
    def __init__(self, name:str, rel_path:str, mamafile:str, always_build:bool, args:list):
        super(LocalSource, self).__init__(name)
        self.is_src = True
        self.rel_path = rel_path
        self.mamafile = mamafile
        self.always_build = always_build
        self.args = args

    def __str__(self):  return f'DepSource LocalSource {self.name} {self.rel_path} {self.mamafile} always_build={self.always_build}'
    def __repr__(self): return self.__str__()

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
