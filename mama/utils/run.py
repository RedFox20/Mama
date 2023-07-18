from __future__ import annotations
from typing import TYPE_CHECKING
import os, shlex
from .system import System
from .sub_process import execute_echo

if TYPE_CHECKING:
    from ..build_target import BuildTarget


def run_in_working_dir(target: BuildTarget, working_dir: str, command: str, exit_on_fail=True):
    shell_args = shlex.split(command)
    executable = shell_args[0]

    dir = os.path.dirname(executable)
    if not dir.startswith('/'):
        dir = './' + dir

    exe = os.path.basename(executable)
    if System.windows and target.windows and '.exe' not in exe:
        exe += '.exe'

    args = ''
    if shell_args:
        args = ' ' + ' '.join(shell_args[1:])

    execute_echo(cwd=working_dir, cmd=f'{dir}/{exe}{args}', exit_on_fail=exit_on_fail)


def run_in_project_dir(target: BuildTarget, command: str, src_dir=False, exit_on_fail=True):
    cwd = target.source_dir() if src_dir else target.build_dir()
    run_in_working_dir(target, cwd, command, exit_on_fail=exit_on_fail)

