from __future__ import annotations
from typing import Tuple, TYPE_CHECKING
import os
from .system import System, console
from .sub_process import execute_echo

if TYPE_CHECKING:
    from ..build_target import BuildTarget


def filter_gdb_arg(args: str, default_gdb=True) -> Tuple[str, bool]:
    if 'gdb' == args: return '', True
    if 'nogdb' == args: return '', False
    if 'gdb ' in args: return args.replace('gdb ', ''), True
    if 'nogdb ' in args: return args.replace('nogdb ', ''), False
    return args, default_gdb


def run_gdb(target: BuildTarget, command: str, src_dir=True):
    if target.android or target.ios or target.raspi or target.oclea or target.mips:
        console('Cannot run tests for Android, iOS, Raspi, Oclea, MIPS builds.')
        return # nothing to run

    split = command.split(' ', 1)
    cmd = split[0].lstrip('.')
    args = split[1] if len(split) >= 2 else ''
    path = target.source_dir() if src_dir else target.build_dir()
    path = f"{path}/{os.path.dirname(cmd).lstrip('/')}"
    exe = os.path.basename(cmd)

    if System.windows and target.windows and '.exe' not in exe:
        exe += '.exe'

    if target.windows:
        if not src_dir:
            path = f'{path}/{target.cmake_build_type}'
        gdb = f'{path}/{exe} {args}'
    elif target.macos:
        # b: batch, q: quiet, -o r: run
        # -k bt: on crash, backtrace
        # -k q: on crash, quit 
        gdb = f'lldb -b -o r -k bt -k q  -- ./{exe} {args}'
    else: # linux
        # r: run;  bt: give backtrace;  q: quit when done;
        gdb = f'gdb -batch -return-child-result -ex=r -ex=bt -ex=q --args ./{exe} {args}'

    if not os.path.exists(f'{path}/{exe}'):
        raise IOError(f'Could not find {path}/{exe}')
    execute_echo(cwd=path, cmd=gdb, exit_on_fail=True)
