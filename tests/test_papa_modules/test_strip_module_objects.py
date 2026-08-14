"""Pins which archive members the module strip removes, and every case where it runs nothing."""
import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from testutils import make_includes_target, write_files

from mama import package
from mama.platforms.windows import Windows
from mama.utils.errors import BuildError


LISTING = 'strview.cpp.o\nrpp-strview.cppm.o\nsprint.cpp.o\n'
LIB_EXE = 'C:/msvc/bin/Hostx64/x64/lib.exe'


def _target(tmp_path, modules=('rpp-strview.cppm',), strip=True, libs=('/pkg/lib/libfoo.a',)):
    target = make_includes_target(str(tmp_path))
    target.exported_includes = [f'{tmp_path}/src']  # a module needs an exported base to ship at all
    target.exported_modules = [f'{tmp_path}/src/rpp/{m}' for m in modules]
    target.exported_libs = list(libs)
    target.strip_module_objects = strip
    target.config.print = False
    return target


def _windows(archiver='', bin_dir='') -> Windows:
    """A Windows platform that answers a fixed archiver, or reads a fixed MSVC toolset dir."""
    platform = Windows(SimpleNamespace(verbose=False))
    platform.msvc_bin64 = lambda: bin_dir
    if archiver: platform.archiver = lambda: archiver
    return platform


def _strip(target, lib='/pkg/lib/libfoo.a', listing=LISTING, status=0):
    """Run the strip with both subprocess primitives stubbed. Returns the SubProcess.run mock."""
    with patch('mama.package.execute_piped_echo', return_value=(status, listing)), \
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
    cmd = _windows(LIB_EXE).remove_from_archive_cmd('foo.lib', ['a.ixx.obj', 'b.ixx.obj'])
    assert cmd == [LIB_EXE, '/NOLOGO', 'foo.lib', '/REMOVE:a.ixx.obj', '/REMOVE:b.ixx.obj']


def test_the_command_keeps_its_arguments_apart(tmp_path):
    # a joined string splits again on every space, so a lib path that holds one loses its arguments
    cmd = _strip(_target(tmp_path), lib='/pkg/my libs/libfoo.a').call_args[0][0]
    assert isinstance(cmd, list) and '/pkg/my libs/libfoo.a' in cmd


def test_a_failed_removal_stops_the_package(tmp_path):
    with patch('mama.package.execute_piped_echo', return_value=(0, LISTING)), \
         patch('mama.package.SubProcess.run', return_value=1):
        with pytest.raises(BuildError):
            package.strip_module_objects(_target(tmp_path), '/pkg/lib/libfoo.a')


def test_a_failed_listing_stops_the_package(tmp_path):
    # an unreadable archive read as empty publishes every module object it holds
    with pytest.raises(BuildError, match='Failed to list'):
        _strip(_target(tmp_path), status=1)


def test_the_listing_goes_through_the_platform_too(tmp_path):
    # a hardcoded `ar t` reads nothing on a toolchain with no ar, and the strip then silently skips
    target = _target(tmp_path)
    with patch('mama.package.execute_piped_echo', return_value=(0, LISTING)) as listed, \
         patch('mama.package.SubProcess.run', return_value=0):
        package.strip_module_objects(target, '/pkg/lib/libfoo.a')
    assert listed.call_args[0][1] == target.config.platform.list_archive_members_cmd('/pkg/lib/libfoo.a')


def test_a_member_name_with_a_space_survives_the_parse(tmp_path):
    # the listing names one member per line, and splitting on whitespace tore such a name in two
    run = _strip(_target(tmp_path, modules=('my module.cppm',)), listing='my module.cppm.o\nother.cpp.o\n')
    assert 'my module.cppm.o' in run.call_args[0][0]


def test_an_object_that_drops_the_module_extension_still_matches(tmp_path):
    # MSVC names it rpp-strview.obj, so a strip that needs the extension does nothing on Windows
    run = _strip(_target(tmp_path), listing='Producer.dir/rpp-strview.obj\nstrview.cpp.obj\n')
    assert run.call_args[0][0].count('Producer.dir/rpp-strview.obj') == 1
    assert 'strview.cpp.obj' not in run.call_args[0][0]


def test_a_bare_object_name_a_second_source_could_own_stays(tmp_path):
    # a foo.o built from foo.cpp carries definitions, and the bare name cannot tell the two apart
    write_files(tmp_path, {'src/rpp/rpp-strview.cpp': '// the same name, not a module'})
    assert not _strip(_target(tmp_path), listing='rpp-strview.o\nsprint.cpp.o\n').called


def test_a_child_package_module_leaves_the_intermediary_archive(tmp_path):
    # a static library that calls mama_target_modules compiles its dependency modules into itself
    child = make_includes_target(str(tmp_path / 'child'))
    child.exported_includes = [f'{tmp_path}/child/src']
    child.exported_modules = [f'{tmp_path}/child/src/rpp/rpp-strview.cppm']
    target = _target(tmp_path, modules=())
    target.children.return_value = [SimpleNamespace(target=child)]
    assert 'rpp-strview.cppm.o' in _strip(target).call_args[0][0]


def test_a_thin_archive_keeps_its_own_path_and_says_so(tmp_path):
    # a thin archive names each member by a path, so a copy resolves every one against the wrong dir
    lib = f'{tmp_path}/libfoo.a'
    open(lib, 'wb').write(b'!<thin>\n//   ')
    target = _target(tmp_path, libs=(lib,))
    with patch('mama.package.warning') as warned:
        _export_stripped(target)
    assert target.exported_libs == [lib]
    assert 'thin archive' in warned.call_args[0][0]


def test_an_archive_that_compiled_no_module_keeps_its_own_path(tmp_path):
    # every target with a module dependency would otherwise publish a copy it never had to strip
    lib = f'{tmp_path}/libfoo.a'
    open(lib, 'w').write('real')
    target = _target(tmp_path, libs=(lib,))
    with patch('mama.package.execute_piped_echo', return_value=(0, 'strview.cpp.o\n')), \
         patch('mama.package.SubProcess.run', return_value=0):
        package.export_stripped_module_libs(target)
    assert target.exported_libs == [lib]


def test_a_private_module_of_the_same_name_survives(tmp_path):
    # both members carry the file name, so only the deeper path tells the exported one apart
    target = _target(tmp_path, modules=('pub/api.cppm',))
    cmd = _strip(target, listing='src/pub/api.cppm.o\nsrc/private/api.cppm.o\n').call_args[0][0]
    assert 'src/pub/api.cppm.o' in cmd and 'src/private/api.cppm.o' not in cmd


def test_the_exact_object_name_wins_over_the_bare_stem(tmp_path):
    # a foo.o built from foo.cpp must not answer for the foo.cppm beside it
    target = _target(tmp_path, modules=('rpp-strview.cppm',))
    cmd = _strip(target, listing='rpp-strview.cppm.o\nrpp-strview.o\n').call_args[0][0]
    assert 'rpp-strview.cppm.o' in cmd and 'rpp-strview.o' not in cmd


def test_a_listing_of_full_object_paths_still_matches(tmp_path):
    # an archiver lists the path it stored, and a compare against that whole line matched nothing
    obj = 'D:\\build\\Producer.dir\\RelWithDebInfo\\rpp-strview.cppm.obj'
    run = _strip(_target(tmp_path), listing=f'{obj}\nD:\\build\\Producer.dir\\strview.cpp.obj\n')
    assert obj in run.call_args[0][0]


def test_the_windows_listing_uses_the_lib_list_flag():
    assert _windows(LIB_EXE).list_archive_members_cmd('foo.lib') == [LIB_EXE, '/NOLOGO', '/LIST', 'foo.lib']


def test_the_windows_archiver_reads_the_msvc_toolset(tmp_path):
    # only a developer prompt puts lib.exe on PATH, and a bare name then fails every build that strips
    bin_dir = f'{tmp_path}/bin/Hostx64/x64/'
    os.makedirs(bin_dir); open(f'{bin_dir}lib.exe', 'w').close()
    assert _windows(bin_dir=bin_dir).archiver() == f'{bin_dir}lib.exe'


def test_the_windows_archiver_falls_back_to_the_path(tmp_path):
    # a host toolset dir that holds no lib.exe leaves PATH as the one place left to look
    assert _windows(bin_dir=f'{tmp_path}/absent/').archiver() == 'lib.exe'


def _export_stripped(target):
    """Run the export swap with both subprocess primitives stubbed."""
    with patch('mama.package.execute_piped_echo', return_value=(0, LISTING)), \
         patch('mama.package.SubProcess.run', return_value=0):
        package.export_stripped_module_libs(target)


def test_a_second_run_strips_the_archive_this_build_wrote(tmp_path):
    # a recorded export names the copy, and reading its own dir nested one dir and republished run 1
    lib = f'{tmp_path}/libfoo.a'
    open(lib, 'w').write('first')
    target = _target(tmp_path, libs=(lib,))
    _export_stripped(target)
    copy = target.exported_libs[0]
    open(lib, 'w').write('rebuilt')  # the next build writes a new archive under the same name
    _export_stripped(target)
    assert target.exported_libs[0] == copy and copy.count('mama-nomodules') == 1
    assert open(copy).read() == 'rebuilt'


def test_a_recorded_export_whose_archive_is_gone_keeps_its_copy(tmp_path):
    # a cleaned build dir leaves the recorded export with no source to read
    target = _target(tmp_path, libs=(f'{tmp_path}/mama-nomodules/libfoo.a',))
    _export_stripped(target)
    assert target.exported_libs == [f'{tmp_path}/mama-nomodules/libfoo.a']




def test_two_members_of_one_name_are_both_removed(tmp_path):
    # `ar d` drops one member per name it is given, so a repeated name has to repeat
    target = _target(tmp_path, modules=('api.cppm',))
    cmd = _strip(target, listing='api.cppm.o\napi.cppm.o\nother.cpp.o\n').call_args[0][0]
    assert cmd.count('api.cppm.o') == 2


def test_a_module_under_no_exported_include_is_never_stripped(tmp_path):
    # it reaches no consumer, so removing its object only loses definitions
    target = _target(tmp_path)
    target.exported_includes = [f'{tmp_path}/elsewhere']
    assert not _strip(target).called


# --- the exported lib a source-built consumer links ---------------------------

def test_the_exported_lib_becomes_a_stripped_copy(tmp_path, monkeypatch):
    # a consumer that builds this dep from source links exported_libs, not the packaged copy
    lib = tmp_path / 'libfoo.a'
    lib.write_bytes(b'\0')
    target = _target(tmp_path, libs=(str(lib),))
    target.dep.from_artifactory = False
    with patch('mama.package.execute_piped_echo', return_value=(0, LISTING)), \
         patch('mama.package.SubProcess.run', return_value=0) as run:
        package.export_stripped_module_libs(target)
    assert target.exported_libs[0].endswith('/mama-nomodules/libfoo.a')
    assert os.path.exists(target.exported_libs[0]), 'the copy is a real file a consumer can link'
    assert 'rpp-strview.cppm.o' in run.call_args[0][0]


def test_the_build_dir_lib_is_never_replaced_for_a_fetched_package(tmp_path):
    # the archive it unpacked is already stripped, so a second copy only costs time
    target = _target(tmp_path)
    target.dep.from_artifactory = True
    package.export_stripped_module_libs(target)
    assert target.exported_libs == ['/pkg/lib/libfoo.a']
