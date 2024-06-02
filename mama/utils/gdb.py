from __future__ import annotations
from typing import Tuple, TYPE_CHECKING
import os
from .system import console, Color
from .run import get_cwd_exe_args
from .sub_process import execute_echo

if TYPE_CHECKING:
    from ..build_target import BuildTarget


def filter_gdb_arg(args: str, default_gdb=False) -> Tuple[str, bool]:
    if 'nogdb' == args: return '', False
    if 'nogdb ' in args: return args.replace('nogdb ', ''), False
    if 'gdb' == args: return '', True
    if 'gdb ' in args: return args.replace('gdb ', ''), True
    return args, default_gdb

def _is_running_leak_sanitizer(target: BuildTarget):
    if target.config.sanitize:
        sanitizers = target.config.sanitize
    else:
        sanitizers = target.dep.get_enabled_sanitizers()
    return ('leak' in sanitizers) or ('address' in sanitizers)


def run_gdb(target: BuildTarget, command: str, src_dir=True):
    if target.android or target.ios or target.raspi or target.oclea or target.mips:
        console('Cannot run tests for Android, iOS, Raspi, Oclea, MIPS builds.')
        return # nothing to run

    root_dir = target.source_dir() if src_dir else target.build_dir()
    if target.windows and not src_dir:
        root_dir = f'{root_dir}/{target.cmake_build_type}'

    cwd, exe, args = get_cwd_exe_args(target, command, root_dir=root_dir)

    if target.windows:
        debugger = f'{exe} {args}'
    elif _is_running_leak_sanitizer(target):
        console('LEAK/ADDRESS sanitizer was enabled - GDB would disable LEAK detection, running without GDB', color=Color.YELLOW)
        debugger = f'{exe} {args}'
    elif target.macos:
        # b: batch, q: quiet, -o r: run
        # -k bt: on crash, backtrace
        # -k q: on crash, quit 
        debugger = f'lldb -b -o r -k bt -k q  -- {exe} {args}'
    else: # linux
        # r: run;  bt: give backtrace;  q: quit when done;
        debugger = f'gdb -batch -return-child-result -ex=r -ex=bt -ex=q --args {exe} {args}'

    if not os.path.exists(exe):
        raise IOError(f'Could not find {exe}')
    execute_echo(cwd=cwd, cmd=debugger, exit_on_fail=True)
