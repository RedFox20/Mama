import os, sys, py_compile, runpy, inspect, pathlib, time
from mama.system import console

def parse_mamafile(config, folder, target_class):
    cmakelists = os.path.join(folder, 'CMakeLists.txt')
    if not os.path.exists(cmakelists):
        raise RuntimeError(f'No CMakeLists found at {cmakelists}. Mamabuild requires a valid CMakeLists')

    mamafile = os.path.join(folder, 'mamafile.py')
    if not os.path.exists(mamafile):
        return None, None

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
        pathlib.Path(tagfile).write_text(str(filetime))
        return True

    tagtime = float(pathlib.Path(tagfile).read_text())
    if filetime != tagtime:
        pathlib.Path(tagfile).write_text(str(filetime))
        return True
    
    return False

## Return: TRUE if mamafile.py was modified
def update_mamafile_tag(src, build_dir):
    mamafile    = os.path.join(src, 'mamafile.py')
    mamafiletag = os.path.join(build_dir, 'mamafile_tag')
    return update_modification_tag(mamafile, mamafiletag)

## Return: TRUE if CMakeLists.txt was modified
def update_cmakelists_tag(src, build_dir):
    cmakelists    = os.path.join(src, 'CMakeLists.txt')
    cmakeliststag = os.path.join(build_dir, 'cmakelists_tag')
    return update_modification_tag(cmakelists, cmakeliststag)
