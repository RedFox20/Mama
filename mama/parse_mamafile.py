import os, sys, py_compile, runpy, inspect

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
