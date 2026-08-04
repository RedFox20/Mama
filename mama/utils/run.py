from __future__ import annotations
from typing import Tuple, TYPE_CHECKING
import os, shlex, shutil
from .sub_process import execute_echo
from .system import System
from ..util import normalized_path, normalized_join

if TYPE_CHECKING:
    from ..build_target import BuildTarget


def get_cwd_exe_args(target: BuildTarget, command: str, cwd='', root_dir='') -> Tuple[str, str, str]:
    """ Extracts the `cwd`, `exe` and `args` from a command string """
    shell_args = shlex.split(command)
    program = shell_args[0]
    args = ' '.join(shell_args[1:]) if shell_args else ''

    # Add or strip the executable suffix. Gated on the HOST as well as the target: a mamafile runs
    # HOST tools during a cross build, so a Windows host must not have `protoc.exe` stripped just
    # because the TARGET platform (android, raspi, mips) has no suffix of its own.
    suffix = target.config.platform.exe_suffix()
    if System.windows and suffix and not program.endswith(suffix): program += suffix
    elif not System.windows and not suffix and program.endswith('.exe'): program = program[:-4]

    if root_dir:
        # the command runs relative to root_dir:
        # program: bin/app.exe
        # cwd: /path/to/root_dir/bin
        # exe: /path/to/root_dir/bin/app.exe
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
        # the command runs in cwd:
        # program: bin/app.exe
        # cwd: /path/to/project
        # exe: /path/to/project/bin/app.exe
        if program.startswith('/'):
            exe = program
        elif program.startswith('./'):
            exe = normalized_join(cwd, program[2:])
        else:
            exe = shutil.which(program) # a program on PATH wins over a cwd-relative one
            if not exe:
                exe = normalized_join(cwd, program)
    else:
        # the command runs in the directory of the executable:
        # program: bin/app.exe
        # cwd: /path/to/bin
        # exe: /path/to/bin/app.exe
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

