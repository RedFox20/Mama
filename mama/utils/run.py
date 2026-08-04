from __future__ import annotations
from typing import Tuple, TYPE_CHECKING
import os, shlex, shutil
from .sub_process import execute_echo
from .system import System
from ..util import normalized_path, normalized_join

if TYPE_CHECKING:
    from ..build_target import BuildTarget


def get_cwd_exe_args(target: BuildTarget, command: str, cwd='', root_dir='') -> Tuple[str, str, str]:
    """Extracts (cwd, exe, args) from a command string, with the platform executable suffix applied.
    target: supplies the platform exe suffix
    command: the command string, shlex-split
    cwd: run the command in this directory
    root_dir: run the command relative to this directory"""
    shell_args = shlex.split(command)
    program = shell_args[0]
    args = ' '.join(shell_args[1:]) if shell_args else ''

    # The suffix is gated on the HOST as well as the target: a mamafile runs HOST tools during a cross
    # build, so a Windows host must not have `protoc.exe` stripped for a suffix-less TARGET platform.
    suffix = target.config.platform.exe_suffix()
    if System.windows and suffix and not program.endswith(suffix): program += suffix
    elif not System.windows and not suffix and program.endswith('.exe'): program = program[:-4]

    if root_dir:
        # relative to root_dir: bin/app.exe -> cwd root_dir/bin, exe root_dir/bin/app.exe
        cwd = normalized_join(root_dir, os.path.dirname(program))
        if program.startswith('/'):
            exe = program
        elif program.startswith('./'):
            exe = normalized_join(root_dir, program[2:])
        else:
            exe = shutil.which(program) # a program on PATH wins over a root_dir-relative one
            if not exe:
                exe = normalized_join(root_dir, program)
    elif cwd:
        # in cwd: bin/app.exe -> cwd unchanged, exe cwd/bin/app.exe
        if program.startswith('/'):
            exe = program
        elif program.startswith('./'):
            exe = normalized_join(cwd, program[2:])
        else:
            exe = shutil.which(program) # a program on PATH wins over a cwd-relative one
            if not exe:
                exe = normalized_join(cwd, program)
    else:
        # in the directory of the executable: bin/app.exe -> cwd /abs/bin, exe /abs/bin/app.exe
        cwd = os.path.dirname(os.path.abspath(program))
        exe = shutil.which(program) # a program on PATH wins over a local one
        if not exe:
            exe = f'{cwd}/{os.path.basename(program)}'

    cwd = normalized_path(cwd)
    exe = normalized_path(exe)
    if ' ' in exe:
        exe = '"' + exe + '"'
    return cwd, exe, args


def run_in_working_dir(target: BuildTarget, working_dir: str, command: str, exit_on_fail=True, env=None):
    cwd, exe, args = get_cwd_exe_args(target, command, cwd=working_dir)
    execute_echo(cwd=cwd, cmd=f'{exe} {args}', exit_on_fail=exit_on_fail, env=env)


def run_in_project_dir(target: BuildTarget, command: str, src_dir=False, exit_on_fail=True, env=None, quiet=False):
    cwd = target.source_dir() if src_dir else target.build_dir()
    cwd, exe, args = get_cwd_exe_args(target, command, cwd=cwd)
    execute_echo(cwd=cwd, cmd=f'{exe} {args}', exit_on_fail=exit_on_fail, env=env, quiet=quiet)


def run_in_command_dir(target: BuildTarget, command: str, src_dir=False, exit_on_fail=True, env=None):
    root_dir = target.source_dir() if src_dir else target.build_dir()
    cwd, exe, args = get_cwd_exe_args(target, command, root_dir=root_dir)
    execute_echo(cwd=cwd, cmd=f'{exe} {args}', exit_on_fail=exit_on_fail, env=env)

