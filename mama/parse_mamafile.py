import os, sys, py_compile, runpy, inspect, pathlib, time
from mama.system import console
from mama.util import path_join

def parse_mamafile(config, folder, target_class, mamafile=None):
    # cmakelists = path_join(folder, 'CMakeLists.txt')
    # if not os.path.exists(cmakelists):
    #     raise RuntimeError(f'No CMakeLists found at {cmakelists}. Mamabuild requires a valid CMakeLists')

    mamafile = mamafile if mamafile else path_join(folder, 'mamafile.py')
    if not os.path.exists(mamafile):
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
def update_mamafile_tag(src, build_dir):
    mamafile    = path_join(src, 'mamafile.py')
    mamafiletag = path_join(build_dir, 'mamafile_tag')
    return update_modification_tag(mamafile, mamafiletag)

## Return: TRUE if CMakeLists.txt was modified
def update_cmakelists_tag(src, build_dir):
    cmakelists    = path_join(src, 'CMakeLists.txt')
    cmakeliststag = path_join(build_dir, 'cmakelists_tag')
    return update_modification_tag(cmakelists, cmakeliststag)
