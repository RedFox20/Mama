import os

class Asset:
    def __init__(self, relpath, fullpath, category):
        """
        Creates an asset. If category is set, it replaces the relative directory in the deploy path.
            relpath  -- Relative path to the source file
            fullpath -- Single full path to the source file
            category -- Deployment category, used as the deploy path prefix
        """
        reldir = os.path.dirname(relpath)
        self.name     = os.path.basename(fullpath)
        self.outpath  = fullpath[fullpath.find(reldir) + len(reldir):].lstrip('\\/')
        self.srcpath  = fullpath

        if category: self.outpath = f'{category}/{self.outpath}'
        else:        self.outpath = f'{reldir}/{self.outpath}'

    def __str__(self):  return self.outpath
    def __repr__(self): return self.outpath
