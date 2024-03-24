import os, runpy, inspect

from .utils.system import console
from .util import path_join, read_text_from, write_text_to

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

def update_modification_tag(config, file, tagfile):
    if not os.path.exists(file):
        return False

    # get the modification time in seconds
    filetime_str = str(int(os.path.getmtime(file)))

    if not os.path.exists(tagfile):
        os.makedirs(os.path.dirname(tagfile), exist_ok=True)
        if config.verbose: console(f'Update tagfile: {tagfile}')
        write_text_to(tagfile, filetime_str)
        return True

    if filetime_str != read_text_from(tagfile):
        if config.verbose: console(f'Update tagfile: {tagfile}')
        write_text_to(tagfile, filetime_str)
        return True

    if config.verbose: console(f'No Changes {file}')
    return False

## Return: TRUE if mamafile.py was modified
def update_mamafile_tag(config, mamafile, build_dir):
    mamafiletag = path_join(build_dir, 'mamafile_tag')
    return update_modification_tag(config, mamafile, mamafiletag)

## Return: TRUE if CMakeLists.txt was modified
def update_cmakelists_tag(config, cmakelists, build_dir):
    cmakeliststag = path_join(build_dir, 'cmakelists_tag')
    return update_modification_tag(config, cmakelists, cmakeliststag)
