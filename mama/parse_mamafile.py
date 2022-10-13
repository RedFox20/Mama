import os, runpy, inspect, pathlib
from .util import path_join

def parse_mamafile(config, target_class, mamafile):
    if not mamafile or not os.path.exists(mamafile):
        return None, None
    #console(f'loaded_mamafile: {mamafile}')

    loaded_globals = runpy.run_path(mamafile)
    for key, value in loaded_globals.items():
        if inspect.isclass(value) and issubclass(value, target_class):
            # print(f'found {key}(BuildTarget): {value}')
            return key, value
    raise RuntimeError(f'No BuildTarget class found in mamafile: {mamafile}')

def update_modification_tag(file, tagfile):
    if not os.path.exists(file):
        return False

    filetime = os.path.getmtime(file)
    if not os.path.exists(tagfile):
        os.makedirs(os.path.dirname(tagfile), exist_ok=True)
        pathlib.Path(tagfile).write_text(str(filetime))
        return True

    tagtime = float(pathlib.Path(tagfile).read_text())
    if filetime != tagtime:
        pathlib.Path(tagfile).write_text(str(filetime))
        return True
    
    return False

## Return: TRUE if mamafile.py was modified
def update_mamafile_tag(mamafile, build_dir):
    mamafiletag = path_join(build_dir, 'mamafile_tag')
    return update_modification_tag(mamafile, mamafiletag)

## Return: TRUE if CMakeLists.txt was modified
def update_cmakelists_tag(cmakelists, build_dir):
    cmakeliststag = path_join(build_dir, 'cmakelists_tag')
    return update_modification_tag(cmakelists, cmakeliststag)
