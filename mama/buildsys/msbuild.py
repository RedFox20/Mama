import os
from mama.platforms.windows import find_vswhere, vswhere_property, VS_ROOTS, VS_VARIANTS
from mama.util import find_executable_from_system
from mama.utils.system import System, console
from mama.build_config import BuildConfig
from mama.utils.sub_process import SubProcess


def find_msbuild(verbose=False) -> str:
    """MSBuild.exe, which ships inside Visual Studio but also stands alone on PATH (dotnet-sdk on
    Linux). PATH wins, so a machine with no Visual Studio still builds."""
    paths = [find_executable_from_system('msbuild')]
    if System.windows:
        if find_vswhere(fail_on_error=False):
            root = vswhere_property('installationPath')
            paths += [f'{root}\\MSBuild\\Current\\Bin\\MSBuild.exe',
                      f'{root}\\MSBuild\\15.0\\Bin\\amd64\\MSBuild.exe']
        # Visual Studio 2017 keeps MSBuild under its own version dir, every later one under Current
        for root in VS_ROOTS:
            sub = 'MSBuild\\15.0\\Bin\\amd64' if root.endswith('2017') else 'MSBuild\\Current\\Bin'
            paths += [f'{root}\\{v}\\{sub}\\MSBuild.exe' for v in VS_VARIANTS]

    for path in paths:
        if path and os.path.exists(path):
            if verbose: console(f'Detected MSBuild: {path}')
            return path
    raise EnvironmentError('Failed to find MSBuild from system PATH.' \
                           ' You can easily configure msbuild by running `mama install-msbuild`.')


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
    msbuild = find_msbuild(config.verbose)
    _check_default_properties(config, properties)

    options_str = _get_msbuild_options(properties)
    if config.verbose: options_str += ' /verbosity:normal'
    elif config.print: options_str += ' /verbosity:minimal'
    else:              options_str += ' /verbosity:quiet'
    
    proj_dir  = os.path.dirname(projectfile)
    proj_file = os.path.basename(projectfile)
    _run_msbuild(f'"{msbuild}" {options_str} "{proj_file}"', proj_dir, config)
    console('')

