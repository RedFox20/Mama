import os
from .system import console
from .build_config import BuildConfig
from mama.utils.sub_process import SubProcess


def _run_msbuild(cmd, cwd, config:BuildConfig):
    if config.verbose:
        console(cmd)

    exit_status = SubProcess.run(cmd, cwd)
    if exit_status == 0:
        return
    raise Exception(f'MSBuild failed with return code {exit_status}')


def _add_if_missing(properties, key, value):
    if not key in properties:
        properties[key] = value


def _check_default_properties(config: BuildConfig, properties: dict):
    if config.release:
        _add_if_missing(properties, 'Configuration', 'Release')
    else:
        _add_if_missing(properties, 'Configuration', 'Debug')
    
    if config.is_target_arch_x64():
        _add_if_missing(properties, 'PreferredToolArchitecture', 'x64')
        _add_if_missing(properties, 'Platform', 'x64')
    elif config.is_target_arch_x86():
        _add_if_missing(properties, 'PreferredToolArchitecture', 'x86')
        _add_if_missing(properties, 'Platform', 'x86')


def _get_msbuild_options(properties):
    result = '/nologo'
    for key, value in properties.items():
        result += f' /p:{key}={value}'
    return result


def msbuild_build(config: BuildConfig, projectfile: str, properties: dict):
    msbuild = config.get_msbuild_path()
    _check_default_properties(config, properties)

    options_str = _get_msbuild_options(properties)
    if config.verbose: options_str += ' /verbosity:normal'
    elif config.print: options_str += ' /verbosity:minimal'
    else:              options_str += ' /verbosity:quiet'
    
    proj_dir  = os.path.dirname(projectfile)
    proj_file = os.path.basename(projectfile)
    _run_msbuild(f'"{msbuild}" {options_str} "{proj_file}"', proj_dir, config)
    console('')

