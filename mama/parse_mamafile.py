import os, sys, py_compile, runpy, inspect
from mama import BuildTarget

def load_build_target(project, mamafile):
    loaded_globals = runpy.run_path(mamafile)
    for key, value in loaded_globals.items():
        if inspect.isclass(value) and issubclass(value, BuildTarget):
            print(f'found {key}(BuildTarget): {value}')
            return value
    raise RuntimeError(f'{project} no BuildTarget class found in mamafile: {mamafile}')

def parse_mamafile(config, folder) -> BuildTarget:
    mamafile = os.path.join(folder, 'mamafile.py')
    project  = os.path.basename(folder)
    if not os.path.exists(mamafile):
        raise RuntimeError(f'{project} no mamafile found at {mamafile}')

    # cmakelists = os.path.join(folder, 'CMakeLists.txt')
    # if not os.path.exists(cmakelists):
    #     raise RuntimeError(f'{project} no CMakeLists found at {cmakelists}. Mamabuild requires a valid CMakeLists')

    buildTarget = load_build_target(project, mamafile)

    buildStatics = buildTarget.__dict__
    workspace = buildStatics['workspace'] if 'workspace' in buildStatics else 'mamabuild'
    target = buildTarget(project, workspace=workspace, config=config)
    return target
