from __future__ import annotations
from typing import TYPE_CHECKING
import os
from .gdb import filter_gdb_arg, run_gdb
from .run import run_in_working_dir

if TYPE_CHECKING:
    from ..build_target import BuildTarget

def run_gtest(target: BuildTarget, executable: str, args='', src_dir=False, gdb=True):
    args, gdb = filter_gdb_arg(args, gdb)
    ## gtest flags:
    # https://github.com/google/googletest/blob/main/googletest/src/gtest.cc#L238
    params = f' --gtest_output="xml:{target.source_dir("test/report.xml")}"'
    if args:
        params += f' --gtest_filter="*{args}*"'

    if gdb:
        run_gdb(target, f'{executable} {params}', src_dir=src_dir)
    else:
        dirname = os.path.dirname(executable)
        exename = os.path.basename(executable)
        dir = target.source_dir(dirname) if src_dir else target.build_dir(dirname)
        run_in_working_dir(target, dir, f'{dir}/{exename} {params}')
