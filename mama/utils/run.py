from __future__ import annotations
from typing import Tuple, TYPE_CHECKING
import os, shlex
from .system import System
from .sub_process import execute_echo
from ..util import normalized_path

if TYPE_CHECKING:
    from ..build_target import BuildTarget


def get_cwd_exe_args(target: BuildTarget, command: str, cwd='', root_dir='') -> Tuple[str, str, str]:
    """ Extracts the `cwd`, `exe` and `args` from a command string """
    shell_args = shlex.split(command)
    program = shell_args[0]
    args = ' '.join(shell_args[1:]) if shell_args else ''

    # add or remove .exe extension
    if System.windows and target.windows and not program.endswith('.exe'):
        program += '.exe'
    if (System.linux or System.macos) and (not target.windows) and program.endswith('.exe'):
        program = program[:-4]

    if root_dir:
        # if root_dir is set, then command will be run relative to it
        # program: bin/app.exe
        # cwd: /path/to/root_dir/bin
        # exe: /path/to/root_dir/bin/app.exe
        cwd = os.path.join(root_dir, os.path.dirname(program))
        if program.startswith('/'): exe = program # already absolute
        else:                       exe = os.path.join(root_dir, program) # turn relative to absolute
        #print(f'ROOT cwd={cwd} exe={exe} args={args}')
    elif cwd:
        # if CWD is set, then command will be run in this dir
        # program: bin/app.exe
        # cwd: /path/to/project
        # exe: /path/to/project/bin/app.exe
        if program.startswith('/'): exe = program # already absolute
        else:                       exe = os.path.join(cwd, program) # turn relative to absolute
        #print(f'CWD cwd={cwd} exe={exe} args={args}')
    else:
        # otherwise the command will be run at the same dir as the executable
        # program: bin/app.exe
        # cwd: /path/to/bin
        # exe: /path/to/bin/app.exe
        cwd = os.path.dirname(os.path.abspath(program))
        exe = f'{cwd}/{os.path.basename(program)}'
        #print(f'DEFAULT cwd={cwd} exe={exe} args={args}')

    cwd = normalized_path(cwd)
    exe = normalized_path(exe)
    return cwd, exe, args


def run_in_working_dir(target: BuildTarget, working_dir: str, command: str, exit_on_fail=True):
    cwd, exe, args = get_cwd_exe_args(target, command, cwd=working_dir)
    execute_echo(cwd=cwd, cmd=f'{exe} {args}', exit_on_fail=exit_on_fail)


def run_in_project_dir(target: BuildTarget, command: str, src_dir=False, exit_on_fail=True):
    cwd = target.source_dir() if src_dir else target.build_dir()
    cwd, exe, args = get_cwd_exe_args(target, command, cwd=cwd)
    execute_echo(cwd=cwd, cmd=f'{exe} {args}', exit_on_fail=exit_on_fail)


def run_in_command_dir(target: BuildTarget, command: str, src_dir=False, exit_on_fail=True):
    root_dir = target.source_dir() if src_dir else target.build_dir()
    cwd, exe, args = get_cwd_exe_args(target, command, root_dir=root_dir)
    execute_echo(cwd=cwd, cmd=f'{exe} {args}', exit_on_fail=exit_on_fail)

