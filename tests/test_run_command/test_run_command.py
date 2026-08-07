"""Pins how `test`, `start` and the gtest helper turn a mamafile command into (cwd, exe, args)."""
from unittest.mock import patch

import pytest

from testutils import make_platform_target
from mama.platforms.linux import Linux
from mama.platforms.windows import Windows
from mama.utils import gdb, gtest, run
from mama.utils.paths import normalized_path


def _target(tmp_path, platform_class=Linux):
    return make_platform_target(tmp_path, platform_class, verbose=False)


def _args(target, command, **kw):
    # shutil.which decides a bare name, and a real `git` on PATH would answer instead of the test tree
    with patch('mama.utils.run.shutil.which', return_value=None):
        return run.get_cwd_exe_args(target, command, **kw)


# --- where the command runs ---------------------------------------------------

def test_a_cwd_run_keeps_the_cwd_and_resolves_the_program_under_it(tmp_path):
    target = _target(tmp_path)
    cwd, exe, args = _args(target, 'bin/app --gtest_list_tests', cwd=target.build_dir())
    assert cwd == target.build_dir()               # the mamafile named the working dir, so it stands
    assert exe == f'{target.build_dir()}/bin/app'
    assert args == '--gtest_list_tests'


def test_a_root_dir_run_moves_the_cwd_to_the_directory_of_the_program(tmp_path):
    # a test binary loads its assets by a path relative to itself, so it has to run from its own dir
    target = _target(tmp_path)
    cwd, exe, _ = _args(target, 'bin/app', root_dir=target.build_dir())
    assert cwd == f'{target.build_dir()}/bin'
    assert exe == f'{target.build_dir()}/bin/app'


def test_a_bare_command_runs_beside_the_program_it_names(tmp_path):
    target = _target(tmp_path)
    program = normalized_path(str(tmp_path / 'tools' / 'gen'))
    cwd, exe, _ = _args(target, f'{program} --help')
    assert cwd == normalized_path(str(tmp_path / 'tools')) and exe == program


@pytest.mark.parametrize('mode', ['cwd', 'root_dir'])
def test_an_absolute_program_is_never_re_rooted(tmp_path, mode):
    target = _target(tmp_path)
    _, exe, _ = _args(target, '/usr/bin/valgrind ./app', **{mode: target.build_dir()})
    assert exe == '/usr/bin/valgrind'


@pytest.mark.parametrize('mode', ['cwd', 'root_dir'])
def test_a_dot_slash_program_resolves_against_the_named_dir(tmp_path, mode):
    target = _target(tmp_path)
    _, exe, _ = _args(target, './app', **{mode: target.build_dir()})
    assert exe == f'{target.build_dir()}/app'


def test_a_program_on_the_path_wins_over_a_local_one(tmp_path):
    target = _target(tmp_path)
    with patch('mama.utils.run.shutil.which', return_value='/usr/bin/ctest'):
        _, exe, _ = run.get_cwd_exe_args(target, 'ctest -V', cwd=target.build_dir())
    assert exe == '/usr/bin/ctest'


def test_a_path_holding_a_space_is_quoted_for_the_shell_split(tmp_path):
    # SubProcess shlex-splits the command, so an unquoted `Program Files` path arrives as two words
    target = _target(tmp_path / 'my project')
    _, exe, _ = _args(target, 'bin/app', cwd=target.build_dir())
    assert exe.startswith('"') and exe.endswith('"') and ' ' in exe


# --- the executable suffix, which follows the HOST as well as the target -------

def test_a_windows_target_gains_the_exe_suffix(tmp_path):
    target = _target(tmp_path, Windows)
    with patch.object(run.System, 'windows', True):
        _, exe, _ = _args(target, 'bin/app', cwd=target.build_dir())
    assert exe.endswith('/bin/app.exe')


def test_a_windows_host_keeps_the_suffix_of_a_host_tool_in_a_cross_build(tmp_path):
    # a mamafile runs HOST tools during a cross build, so a suffix-less TARGET must not strip protoc.exe
    target = _target(tmp_path, Linux)
    with patch.object(run.System, 'windows', True):
        _, exe, _ = _args(target, 'bin/protoc.exe', cwd=target.build_dir())
    assert exe.endswith('/bin/protoc.exe')


def test_a_unix_host_strips_an_exe_suffix_the_mamafile_hardcoded(tmp_path):
    target = _target(tmp_path, Linux)
    with patch.object(run.System, 'windows', False):
        _, exe, _ = _args(target, 'bin/app.exe', cwd=target.build_dir())
    assert exe.endswith('/bin/app')


# --- the gdb token, which `mama build test=<args>` accepts ---------------------

@pytest.mark.parametrize('args, expect', [
    ('gdb',              ('', True)),
    ('gdb --filter x',   ('--filter x', True)),
    ('nogdb',            ('', False)),
    ('nogdb --filter x', ('--filter x', False)),
    ('--filter x',       ('--filter x', False)),   # neither token, so the default decides
    ('',                 ('', False)),
])
def test_the_gdb_token_is_stripped_and_decides_the_debugger(args, expect):
    assert gdb.filter_gdb_arg(args) == expect


def test_the_default_stands_when_the_args_choose_neither():
    assert gdb.filter_gdb_arg('--filter x', default_gdb=True) == ('--filter x', True)
    assert gdb.filter_gdb_arg('nogdb --filter x', default_gdb=True) == ('--filter x', False)


# --- the gtest helper: `mama build test=<args>` becomes a gtest filter ---------

def _gtest_command(tmp_path, args=''):
    """The command line run_gtest would have run, with the process spawn stubbed."""
    ran = {}
    target = _target(tmp_path)
    with patch('mama.utils.gtest.run_in_working_dir', side_effect=lambda t, d, c: ran.update(dir=d, cmd=c)):
        gtest.run_gtest(target, 'bin/tests', args=args)
    return ran['cmd']


def test_a_bare_word_becomes_a_wildcard_gtest_filter(tmp_path):
    # `mama build test=socket` means "run the tests whose name holds socket", not an exact match
    assert '--gtest_filter="*socket*"' in _gtest_command(tmp_path, 'socket')


def test_several_words_join_into_one_filter(tmp_path):
    assert '--gtest_filter="*socket*:*buffer*"' in _gtest_command(tmp_path, 'socket buffer')


@pytest.mark.parametrize('quoted', ['--gtest_filter="Net.*"', "--gtest_filter='Net.*'"])
def test_an_explicit_filter_passes_through_unquoted(tmp_path, quoted):
    # the user already spelled a gtest pattern, so wrapping it in wildcards would change what it matches
    assert '--gtest_filter="Net.*"' in _gtest_command(tmp_path, quoted)


def test_an_explicit_filter_merges_with_a_bare_word(tmp_path):
    assert '--gtest_filter="Net.*:*socket*"' in _gtest_command(tmp_path, '--gtest_filter="Net.*" socket')


def test_another_gtest_flag_reaches_the_binary_and_never_the_filter(tmp_path):
    cmd = _gtest_command(tmp_path, '--gtest_list_tests')
    assert '--gtest_list_tests' in cmd and '--gtest_filter' not in cmd


def test_no_args_asks_for_no_filter_at_all(tmp_path):
    assert '--gtest_filter' not in _gtest_command(tmp_path)


def test_the_report_always_lands_in_the_source_tree(tmp_path):
    # a build dir is disposable, and the CI step that collects the report reads the source tree
    assert f'--gtest_output="xml:{normalized_path(str(tmp_path / "src"))}/test/report.xml"' \
        in _gtest_command(tmp_path)


def test_a_gdb_token_in_the_test_args_routes_to_the_debugger(tmp_path):
    target = _target(tmp_path)
    with patch('mama.utils.gtest.run_gdb') as run_gdb, \
         patch('mama.utils.gtest.run_in_working_dir') as plain:
        gtest.run_gtest(target, 'bin/tests', args='gdb socket')
    plain.assert_not_called()
    assert '--gtest_filter="*socket*"' in run_gdb.call_args.args[1]   # the filter survives the reroute
