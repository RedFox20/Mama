import os

class Asset:
    def __init__(self, relpath, fullpath, category):
        """
        Creates an asset. If category is set, then relpath is ignored during deploy
            relpath  -- Relative path to source
            fullpath -- Single full path
            category -- Deployment category
        """
        reldir = os.path.dirname(relpath)
        self.name     = os.path.basename(fullpath)
        self.outpath  = fullpath[fullpath.find(reldir) + len(reldir):].lstrip('\\/')
        self.srcpath  = fullpath

        if category: self.outpath = f'{category}/{self.outpath}'
        else:        self.outpath = f'{reldir}/{self.outpath}'
        #console(f'asset {self.outpath}')

    def __str__(self):  return self.outpath
    def __repr__(self): return self.outpath
