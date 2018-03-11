import os, sys, py_compile, runpy, inspect, pathlib, time
from mama.system import console

def load_build_target(project, mamafile, target_class):
    loaded_globals = runpy.run_path(mamafile)
    for key, value in loaded_globals.items():
        if inspect.isclass(value) and issubclass(value, target_class):
            # print(f'found {key}(BuildTarget): {value}')
            return key, value
    raise RuntimeError(f'{project} no BuildTarget class found in mamafile: {mamafile}')

def parse_mamafile(config, folder, target_class):
    mamafile = os.path.join(folder, 'mamafile.py')
    project  = os.path.basename(folder)
    if not os.path.exists(mamafile):
        raise RuntimeError(f'{project} no mamafile found at {mamafile}')

    # cmakelists = os.path.join(folder, 'CMakeLists.txt')
    # if not os.path.exists(cmakelists):
    #     raise RuntimeError(f'{project} no CMakeLists found at {cmakelists}. Mamabuild requires a valid CMakeLists')

    return load_build_target(project, mamafile, target_class)

## Return: TRUE if mamafile.py was modified
def update_mamafile_tag(src, build_dir):
    mamafile = os.path.join(src, 'mamafile.py')
    mamafiletag = os.path.join(build_dir, 'mamafile_tag')

    mf_time = os.path.getmtime(mamafile)

    if not os.path.exists(mamafiletag):
        pathlib.Path(mamafiletag).write_text(str(mf_time))
        return True

    tag_time = float(pathlib.Path(mamafiletag).read_text())

    if mf_time != tag_time:
        pathlib.Path(mamafiletag).write_text(str(mf_time))
        return True
    
    return False
