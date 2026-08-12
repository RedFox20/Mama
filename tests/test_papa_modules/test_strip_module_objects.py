"""Pins which archive members the module strip removes, and every case where it runs nothing."""
from unittest.mock import patch

import pytest
from testutils import make_includes_target

from mama import package
from mama.platforms.windows import Windows
from mama.utils.errors import BuildError


LISTING = 'strview.cpp.o\nrpp-strview.cppm.o\nsprint.cpp.o\n'


def _target(tmp_path, modules=('rpp-strview.cppm',), strip=True):
    target = make_includes_target(str(tmp_path))
    target.exported_modules = [f'{tmp_path}/src/rpp/{m}' for m in modules]
    target.strip_module_objects = strip
    target.config.print = False
    return target


def _strip(target, lib='/pkg/lib/libfoo.a', listing=LISTING):
    """Run the strip with both subprocess primitives stubbed. Returns the SubProcess.run mock."""
    with patch('mama.package.execute_piped', return_value=listing), \
         patch('mama.package.SubProcess.run', return_value=0) as run:
        package.strip_module_objects(target, lib)
    return run


def test_only_the_module_objects_reach_the_remove_command(tmp_path):
    run = _strip(_target(tmp_path))
    cmd = run.call_args[0][0]
    assert 'rpp-strview.cppm.o' in cmd
    assert 'strview.cpp.o' not in cmd and 'sprint.cpp.o' not in cmd


def test_an_archive_holding_no_module_object_runs_nothing(tmp_path):
    assert not _strip(_target(tmp_path), listing='strview.cpp.o\n').called


def test_the_opt_out_runs_nothing(tmp_path):
    assert not _strip(_target(tmp_path, strip=False)).called


def test_a_target_without_modules_runs_nothing(tmp_path):
    assert not _strip(_target(tmp_path, modules=())).called


def test_a_dynamic_library_runs_nothing(tmp_path):
    assert not _strip(_target(tmp_path), lib='/pkg/lib/libfoo.so').called


def test_an_msvc_object_suffix_still_matches(tmp_path):
    run = _strip(_target(tmp_path, modules=('rpp-strview.ixx',)), listing='rpp-strview.ixx.obj\n')
    assert 'rpp-strview.ixx.obj' in run.call_args[0][0]


def test_the_cross_archiver_prefix_reaches_the_command(tmp_path):
    target = _target(tmp_path)
    target.config.platform.toolchain().tool_prefix = '/opt/sdk/bin/aarch64-linux-gnu-'
    assert '/opt/sdk/bin/aarch64-linux-gnu-ar' in _strip(target).call_args[0][0]


def test_the_windows_command_uses_the_lib_remove_flag():
    cmd = Windows.remove_from_archive_cmd(None, 'foo.lib', ['a.ixx.obj', 'b.ixx.obj'])
    assert cmd == ['lib.exe', '/NOLOGO', 'foo.lib', '/REMOVE:a.ixx.obj', '/REMOVE:b.ixx.obj']


def test_the_command_keeps_its_arguments_apart(tmp_path):
    # a joined string splits again on every space, so a lib path that holds one loses its arguments
    cmd = _strip(_target(tmp_path), lib='/pkg/my libs/libfoo.a').call_args[0][0]
    assert isinstance(cmd, list) and '/pkg/my libs/libfoo.a' in cmd


def test_a_failed_removal_stops_the_package(tmp_path):
    with patch('mama.package.execute_piped', return_value=LISTING), \
         patch('mama.package.SubProcess.run', return_value=1):
        with pytest.raises(BuildError):
            package.strip_module_objects(_target(tmp_path), '/pkg/lib/libfoo.a')


def test_the_listing_goes_through_the_platform_too(tmp_path):
    # a hardcoded `ar t` reads nothing on a toolchain with no ar, and the strip then silently skips
    target = _target(tmp_path)
    with patch('mama.package.execute_piped', return_value=LISTING) as listed, \
         patch('mama.package.SubProcess.run', return_value=0):
        package.strip_module_objects(target, '/pkg/lib/libfoo.a')
    assert listed.call_args[0][0] == target.config.platform.list_archive_members_cmd('/pkg/lib/libfoo.a')


def test_the_windows_listing_uses_the_lib_list_flag():
    assert Windows.list_archive_members_cmd(None, 'foo.lib') == ['lib.exe', '/NOLOGO', '/LIST', 'foo.lib']
