"""Pins which archive members the module strip removes, and every case where it runs nothing."""
import contextlib
import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from testutils import make_includes_target, write_files

from mama import package
from mama.platforms.windows import Windows
from mama.utils.errors import BuildError
from mama.utils.paths import forward_slashes


LISTING = 'strview.cpp.o\nrpp-strview.cppm.o\nsprint.cpp.o\n'
LIB_EXE = 'C:/msvc/bin/Hostx64/x64/lib.exe'
NOMOD = package.MODULE_STRIP_DIR


def _target(tmp_path, modules=('rpp-strview.cppm',), strip=True, libs=('/pkg/lib/libfoo.a',),
            root=None, build_dir=None):
    """A target exporting `modules` under its src dir. `build_dir` puts the build tree outside it."""
    root = root or f'{tmp_path}/src'
    target = make_includes_target(root if build_dir else str(tmp_path), build_dir=build_dir)
    target.exported_includes = [root]  # a module needs an exported base to ship at all
    target.exported_modules = [f'{root}/{m}' for m in modules]
    target.exported_libs = list(libs)
    target.strip_module_objects = strip
    return target


def _child(root, modules, children=(), strip=True):
    """A dependency target exporting `modules`, wrapped the way `children()` answers it."""
    target = make_includes_target(root)
    target.strip_module_objects = strip
    target.exported_includes = [f'{root}/src']
    target.exported_modules = [f'{root}/src/{m}' for m in modules]
    target.children.return_value = list(children)
    return SimpleNamespace(name=os.path.basename(root), target=target)


@contextlib.contextmanager
def _stubs(listing=LISTING, status=0, run_status=0):
    """Both subprocess primitives of the strip, stubbed. Yields (the listing mock, the run mock)."""
    with patch('mama.package.execute_piped_echo', return_value=(status, listing)) as listed, \
         patch('mama.package.SubProcess.run', return_value=run_status) as run:
        yield listed, run


def _strip(target, lib='/pkg/lib/libfoo.a', **kw):
    """Run the strip with both primitives stubbed. Returns the SubProcess.run mock."""
    with _stubs(**kw) as (_, run):
        package.strip_module_objects(target, lib)
    return run


def _export_stripped(target, **kw):
    """Run the export swap with both primitives stubbed. Returns the SubProcess.run mock."""
    with _stubs(**kw) as (_, run):
        package.export_stripped_module_libs(target)
    return run


def _windows(archiver='', bin_dir='') -> Windows:
    """A Windows platform that answers a fixed archiver, or reads a fixed MSVC toolset dir."""
    platform = Windows(SimpleNamespace(verbose=False))
    platform.msvc_bin64 = lambda: bin_dir
    if archiver: platform.archiver = lambda: archiver
    return platform


# --- which members the strip takes -------------------------------------------

def test_only_the_module_objects_reach_the_remove_command(tmp_path):
    cmd = _strip(_target(tmp_path)).call_args[0][0]
    assert 'rpp-strview.cppm.o' in cmd
    assert 'strview.cpp.o' not in cmd and 'sprint.cpp.o' not in cmd


@pytest.mark.parametrize('modules, listing, member', [
    # MSVC keeps the module extension on the object, and the strip must read that spelling too
    (('rpp-strview.ixx',), 'rpp-strview.ixx.obj\n', 'rpp-strview.ixx.obj'),
    # the listing names one member per line, and splitting on whitespace tore such a name in two
    (('my module.cppm',), 'my module.cppm.o\nother.cpp.o\n', 'my module.cppm.o'),
    # MSVC also drops it, so a strip that needs the extension does nothing on Windows
    (('rpp-strview.cppm',), 'Producer.dir/rpp-strview.obj\nstrview.cpp.obj\n',
     'Producer.dir/rpp-strview.obj'),
    # an archiver lists the path it stored, and a compare against that whole line matched nothing
    (('rpp-strview.cppm',), 'D:\\b\\rpp-strview.cppm.obj\nD:\\b\\strview.cpp.obj\n',
     'D:\\b\\rpp-strview.cppm.obj'),
])
def test_the_strip_matches_the_object_name_the_archiver_wrote(tmp_path, modules, listing, member):
    cmd = _strip(_target(tmp_path, modules=modules), listing=listing).call_args[0][0]
    assert cmd.count(member) == 1


@pytest.mark.parametrize('modules, listing, taken, left', [
    # both members carry the file name, so only the deeper path tells the exported one apart
    (('pub/api.cppm',), 'src/pub/api.cppm.o\nsrc/private/api.cppm.o\n',
     'src/pub/api.cppm.o', 'src/private/api.cppm.o'),
    # a foo.o built from foo.cpp must not answer for the foo.cppm beside it
    (('rpp-strview.cppm',), 'rpp-strview.cppm.o\nrpp-strview.o\n',
     'rpp-strview.cppm.o', 'rpp-strview.o'),
])
def test_a_deeper_path_beats_a_name_a_second_unit_shares(tmp_path, modules, listing, taken, left):
    cmd = _strip(_target(tmp_path, modules=modules), listing=listing).call_args[0][0]
    assert taken in cmd and left not in cmd


def test_two_exported_modules_of_one_name_both_lose_their_objects(tmp_path):
    # the archiver flattens both to api.cppm.o, and a lookup that kept one path called the other private
    write_files(tmp_path, {'src/a/api.cppm': 'module a;\n', 'src/b/api.cppm': 'module b;\n'})
    target = _target(tmp_path, modules=('a/api.cppm', 'b/api.cppm'))
    cmd = _strip(target, listing='api.cppm.o\napi.cppm.o\nother.cpp.o\n').call_args[0][0]
    assert cmd.count('api.cppm.o') == 2  # `ar d` drops one member per name it is given


# --- and every case where it runs nothing ------------------------------------

@pytest.mark.parametrize('kw, lib, listing, files', [
    ({}, None, 'strview.cpp.o\n', None),                       # the archive holds no module object
    ({'strip': False}, None, LISTING, None),                   # the opt-out
    ({'modules': ()}, None, LISTING, None),                    # the target exports no module
    ({}, '/pkg/lib/libfoo.so', LISTING, None),                 # a dynamic library
    # a foo.o built from foo.cpp carries definitions, and the bare name cannot tell the two apart
    ({}, None, 'rpp-strview.o\nsprint.cpp.o\n', {'src/rpp-strview.cpp': '// the same name'}),
    # MSVC drops the module extension, and a generated foo.cpp of that name lands in the build dir
    ({'modules': ('api.cppm',)}, None, 'api.obj\n', {'build/gen/api.cpp': '// generated'}),
])
def test_the_strip_runs_nothing(tmp_path, kw, lib, listing, files):
    if files: write_files(tmp_path, files)
    assert not _strip(_target(tmp_path, **kw), lib=lib or '/pkg/lib/libfoo.a', listing=listing).called


def test_a_module_under_no_exported_include_is_never_stripped(tmp_path):
    # it reaches no consumer, so removing its object only loses definitions
    target = _target(tmp_path)
    target.exported_includes = [f'{tmp_path}/elsewhere']
    assert not _strip(target).called


@pytest.mark.parametrize('modules, listing, files, warns', [
    # GNU ar stores no path, so this one member could be either unit, and only one of them ships
    (('api.cppm',), 'api.cppm.o\nother.cpp.o\n',
     {'src/private/api.cppm': 'module;', 'src/api.cppm': 'module;'}, 'api.cppm'),
    # removing both would take the definitions of the private unit, and the consumer link then fails
    (('api.cppm',), 'api.cppm.o\napi.cppm.o\nother.cpp.o\n', None, 'api.cppm.o'),
])
def test_a_member_no_module_can_claim_stays_and_warns(tmp_path, modules, listing, files, warns):
    if files: write_files(tmp_path, files)
    with patch('mama.package.warning') as warned:
        assert not _strip(_target(tmp_path, modules=modules), listing=listing).called
    assert warns in warned.call_args[0][0]


# --- the build tree, which holds units this target never exported -------------

def _apart(tmp_path, files, exported='module rpp.api;\n'):
    """A target whose build dir sits outside its source tree, holding `files`. Exports src/rpp/api.cppm."""
    write_files(f'{tmp_path}/src', {'rpp/api.cppm': exported})
    write_files(f'{tmp_path}/out', files)
    return _target(tmp_path, modules=('rpp/api.cppm',), root=f'{tmp_path}/src',
                   build_dir=f'{tmp_path}/out')


def test_a_generated_module_of_the_same_name_keeps_the_object(tmp_path):
    # a generated private api.cppm owns its own member, and a bare listing names it like the exported one
    target = _apart(tmp_path, {'gen/api.cppm': 'module gen.api;\n'})
    with patch('mama.package.warning') as warned:
        assert not _strip(target, listing='api.cppm.o\n').called
    assert 'api.cppm' in warned.call_args[0][0]


@pytest.mark.parametrize('files', [
    # an install step writes the exported module into the build tree, and that copy is ours
    {'src/rpp/api.cppm': 'module rpp.api;\n'},
    # a deployed package holds its own modules, and its tree answers for them through its own records
    {'deploy/TestLib/papa.txt': 'P TestLib\n', 'deploy/TestLib/include/rpp/api.cppm': 'drifted\n'},
])
def test_a_build_tree_copy_of_our_own_module_still_strips(tmp_path, files):
    cmd = _strip(_apart(tmp_path, files), listing='api.cppm.o\n').call_args[0][0]
    assert 'api.cppm.o' in cmd


# --- the modules of every package below this one ------------------------------

def test_a_child_package_module_leaves_the_intermediary_archive(tmp_path):
    # a static library that calls mama_target_modules compiles its dependency modules into itself
    target = _target(tmp_path, modules=())
    target.children.return_value = [_child(f'{tmp_path}/child', ['rpp/rpp-strview.cppm'])]
    assert 'rpp-strview.cppm.o' in _strip(target).call_args[0][0]


def test_a_grandchild_package_module_leaves_the_intermediary_archive(tmp_path):
    # MAMA_MODULES aggregates the whole dep tree, so this archive compiled the grandchild module too
    grand = _child(f'{tmp_path}/grand', ['rpp/rpp-vec.cppm'])
    target = _target(tmp_path, modules=())
    target.children.return_value = [_child(f'{tmp_path}/child', [], children=[grand])]
    assert 'rpp-vec.cppm.o' in _strip(target, listing='rpp-vec.cppm.o\n').call_args[0][0]


def test_a_nested_child_module_is_not_a_private_unit(tmp_path):
    # a local child dep sits under the parent source dir, and the walk finds its module there. An
    # identity map of the parent exports alone called the child's own object private.
    write_files(tmp_path, {'child/src/api.cppm': 'module child.api;\n'})
    target = _target(tmp_path, modules=())
    target.children.return_value = [_child(f'{tmp_path}/child', ['api.cppm'])]
    assert 'api.cppm.o' in _strip(target, listing='api.cppm.o\nother.cpp.o\n').call_args[0][0]


# --- the command, and what a failure does ------------------------------------

def test_the_cross_archiver_prefix_reaches_the_command(tmp_path):
    target = _target(tmp_path)
    target.config.platform.toolchain().tool_prefix = '/opt/sdk/bin/aarch64-linux-gnu-'
    assert '/opt/sdk/bin/aarch64-linux-gnu-ar' in _strip(target).call_args[0][0]


def test_the_command_keeps_its_arguments_apart(tmp_path):
    # a joined string splits again on every space, so a lib path that holds one loses its arguments
    cmd = _strip(_target(tmp_path), lib='/pkg/my libs/libfoo.a').call_args[0][0]
    assert isinstance(cmd, list) and '/pkg/my libs/libfoo.a' in cmd


@pytest.mark.parametrize('listing, mode', [
    # `ar d` matches the base name, so an archive that stored a path kept the member and exited 0
    ('src/pub/rpp-strview.cppm.o\nsrc/pub/strview.cpp.o\n', 'dP'),
    # `P` compares the whole stored name, and an archive of bare names then matches no path
    (LISTING, 'd'),
])
def test_the_delete_mode_follows_the_names_the_listing_printed(tmp_path, listing, mode):
    assert _strip(_target(tmp_path), listing=listing).call_args[0][0][1] == mode


def test_a_failed_removal_stops_the_package(tmp_path):
    with pytest.raises(BuildError):
        _strip(_target(tmp_path), run_status=1)


def test_a_failed_listing_stops_the_package(tmp_path):
    # an unreadable archive read as empty publishes every module object it holds
    with pytest.raises(BuildError, match='Failed to list'):
        _strip(_target(tmp_path), status=1)


def test_the_listing_goes_through_the_platform_too(tmp_path):
    # a hardcoded `ar t` reads nothing on a toolchain with no ar, and the strip then silently skips
    target = _target(tmp_path)
    with _stubs() as (listed, _):
        package.strip_module_objects(target, '/pkg/lib/libfoo.a')
    assert listed.call_args[0][1] == target.config.platform.list_archive_members_cmd('/pkg/lib/libfoo.a')


# --- the Windows archiver ----------------------------------------------------

def test_the_windows_commands_use_the_lib_flags():
    assert _windows(LIB_EXE).list_archive_members_cmd('foo.lib') == [LIB_EXE, '/NOLOGO', '/LIST', 'foo.lib']
    assert _windows(LIB_EXE).remove_from_archive_cmd('foo.lib', ['a.obj', 'b.obj']) \
        == [LIB_EXE, '/NOLOGO', 'foo.lib', '/REMOVE:a.obj', '/REMOVE:b.obj']


@pytest.mark.parametrize('has_lib_exe', [True, False])
def test_the_windows_archiver_reads_the_msvc_toolset_before_the_path(tmp_path, has_lib_exe):
    # only a developer prompt puts lib.exe on PATH, and a bare name then fails every build that strips
    bin_dir = f'{tmp_path}/bin/Hostx64/x64/'
    if has_lib_exe: os.makedirs(bin_dir); open(f'{bin_dir}lib.exe', 'w').close()
    expected = f'{bin_dir}lib.exe' if has_lib_exe else 'lib.exe'
    assert _windows(bin_dir=bin_dir).archiver() == expected


@pytest.mark.parametrize('folds_case', [True, False])
def test_an_uppercase_archive_suffix_follows_the_filesystem(folds_case):
    # a Windows recipe may spell it Producer.LIB, and only a case-folding filesystem reads one file
    with patch.object(package.System, 'windows', folds_case), \
         patch.object(package.System, 'macos', False), patch.object(package.System, 'linux', not folds_case):
        assert package.is_a_static_library('Producer.LIB') is folds_case


# --- the exported lib a source-built consumer links ---------------------------

def test_the_exported_lib_becomes_a_stripped_copy(tmp_path):
    # a consumer that builds this dep from source links exported_libs, not the packaged copy
    write_files(str(tmp_path), {'libfoo.a': '\0'})
    target = _target(tmp_path, libs=(f'{tmp_path}/libfoo.a',))
    run = _export_stripped(target)
    assert target.exported_libs[0].endswith(f'/{NOMOD}/libfoo.a')
    assert os.path.exists(target.exported_libs[0]), 'the copy is a real file a consumer can link'
    assert 'rpp-strview.cppm.o' in run.call_args[0][0]


def test_a_second_run_strips_the_archive_this_build_wrote(tmp_path):
    # a recorded export names the copy, and reading its own dir nested one dir and republished run 1
    lib = f'{tmp_path}/libfoo.a'
    open(lib, 'w').write('first')
    target = _target(tmp_path, libs=(lib,))
    _export_stripped(target)
    copy = target.exported_libs[0]
    open(lib, 'w').write('rebuilt')  # the next build writes a new archive under the same name
    _export_stripped(target)
    assert target.exported_libs[0] == copy and copy.count(NOMOD) == 1
    assert open(copy).read() == 'rebuilt'


@pytest.mark.parametrize('kw, files, listing, keeps_copy', [
    # a rebuild that compiles no module must undo the copy an earlier run recorded
    ({}, {'libfoo.a': 'x', f'{NOMOD}/libfoo.a': 'stale'}, 'other.cpp.o\n', False),
    # every target with a module dependency would otherwise publish a copy it never had to strip
    ({}, {'libfoo.a': 'real'}, 'strview.cpp.o\n', False),
    # this rebuild compiles no module at all, and the recorded export names the stripped copy
    ({'modules': ()}, {'libfoo.a': 'x'}, LISTING, False),
    # a cleaned build dir leaves the recorded export with no source to read
    ({}, {}, LISTING, True),
])
def test_the_export_points_at_the_archive_this_build_wrote(tmp_path, kw, files, listing, keeps_copy):
    if files: write_files(str(tmp_path), files)
    root = forward_slashes(str(tmp_path))
    copy = f'{root}/{NOMOD}/libfoo.a'
    target = _target(tmp_path, libs=(copy,), **kw)
    _export_stripped(target, listing=listing)
    assert target.exported_libs[0] == (copy if keeps_copy else f'{root}/libfoo.a')


@pytest.mark.parametrize('recorded_copy', [False, True])
def test_a_thin_archive_keeps_its_own_path_and_says_so(tmp_path, recorded_copy):
    # a thin archive names each member by a path, so a copy resolves every one against the wrong dir
    root = forward_slashes(str(tmp_path))  # an export always carries forward slashes, on every host
    lib = f'{root}/libfoo.a'
    open(lib, 'wb').write(b'!<thin>\n//   ')
    if recorded_copy: os.makedirs(f'{root}/{NOMOD}', exist_ok=True)
    target = _target(tmp_path, libs=(f'{root}/{NOMOD}/libfoo.a' if recorded_copy else lib,))
    with patch('mama.package.warning') as warned:
        _export_stripped(target)
    assert target.exported_libs == [lib]
    assert 'thin archive' in warned.call_args[0][0]


def test_the_build_dir_lib_is_never_replaced_for_a_fetched_package(tmp_path):
    # the archive it unpacked is already stripped, so a second copy only costs time
    target = _target(tmp_path)
    target.dep.from_artifactory = True
    package.export_stripped_module_libs(target)
    assert target.exported_libs == ['/pkg/lib/libfoo.a']


def test_a_directory_of_that_name_without_the_original_is_not_a_stripped_copy(tmp_path):
    # a project may keep its own mama-nomodules/ dir, and only the archive beside it names ours
    lib = f'{tmp_path}/{NOMOD}/libfoo.a'
    assert package._unstripped_lib(lib) == lib
    write_files(str(tmp_path), {'libfoo.a': '\0'})
    assert package._unstripped_lib(lib) == f'{forward_slashes(str(tmp_path))}/libfoo.a'


def test_a_private_module_keeps_its_object_when_msvc_drops_the_extension(tmp_path):
    # MSVC names it api.obj, and the fallback probes the bare stem, which the record has to carry
    write_files(tmp_path, {'src/priv/api.cppm': 'module priv.api;\n'})
    target = _target(tmp_path, modules=('pub/api.cppm',))
    assert not _strip(target, listing='api.obj\nother.cpp.obj\n').called


@pytest.mark.case_sensitive_fs
def test_a_case_distinct_private_module_keeps_its_object(tmp_path):
    # a case-sensitive volume keeps Include/ and include/ apart, and folding read the private unit
    # as the exported one, so nothing stopped the strip from taking its definitions
    write_files(tmp_path, {'Include/api.cppm': 'module pub.api;\n', 'include/api.cppm': 'module priv;\n'})
    target = _target(tmp_path, modules=('api.cppm',), root=f'{tmp_path}/Include')
    with patch.object(package.System, 'macos', True):  # the platform that folds case
        assert not _strip(target, listing='api.cppm.o\nother.cpp.o\n').called


def test_a_child_that_opted_out_keeps_its_object_in_the_parent_archive(tmp_path):
    # strip_objects=False says the module holds a definition no other archive carries. A parent that
    # compiles that module owns the only copy, so reading the parent flag alone dropped the definition.
    target = _target(tmp_path, modules=())
    target.children.return_value = [_child(f'{tmp_path}/child', ['rpp/rpp-strview.cppm'], strip=False)]
    assert not _strip(target).called
