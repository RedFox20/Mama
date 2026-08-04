import os

class Asset:
    def __init__(self, relpath, fullpath, category):
        """Create an asset.
        relpath: relative path to the source file
        fullpath: full path to the source file
        category: deploy path prefix that replaces the relative directory when set
        """
        reldir = os.path.dirname(relpath)
        self.name     = os.path.basename(fullpath)
        self.outpath  = fullpath[fullpath.find(reldir) + len(reldir):].lstrip('\\/')
        self.srcpath  = fullpath

        if category: self.outpath = f'{category}/{self.outpath}'
        else:        self.outpath = f'{reldir}/{self.outpath}'

    def __str__(self):  return self.outpath
    def __repr__(self): return self.outpath
