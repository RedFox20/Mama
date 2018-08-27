from .system import System, console
from .build_config import BuildConfig
from .async_file_reader import AsyncFileReader
import subprocess, os


def _run_msbuild(cwd, args, config:BuildConfig):
    if config.verbose:
        console(args)
    proc = subprocess.Popen(args, shell=True, universal_newlines=True, cwd=cwd)
    retcode = proc.wait()
    if retcode == 0:
        return
    raise Exception(f'MSBuild failed with return code {retcode}')


def _add_if_missing(properties, key, value):
    if not key in properties:
        properties[key] = value


def _check_default_properties(properties:dict):
    _add_if_missing(properties, 'PreferredToolArchitecture', 'x64')
    _add_if_missing(properties, 'Configuration', 'Release')
    _add_if_missing(properties, 'Platform', 'x64')


def _get_msbuild_options(properties):
    result = '/nologo'
    for key, value in properties.items():
        result += f' /p:{key}={value}'
    return result


def msbuild_build(config:BuildConfig, projectfile:str, properties:dict):
    if config.print:
        console('\n#########################################')
        console(f'MSBuild {projectfile}')

    msbuild = config.get_msbuild_path()
    _check_default_properties(properties)

    options_str = _get_msbuild_options(properties)
    if config.verbose: options_str += ' /verbosity:normal'
    elif config.print: options_str += ' /verbosity:minimal'
    else:              options_str += ' /verbosity:quiet'
    
    proj_dir  = os.path.dirname(projectfile)
    proj_file = os.path.basename(projectfile)
    _run_msbuild(proj_dir, f'"{msbuild}" {options_str} "{proj_file}"', config)
    console('')

