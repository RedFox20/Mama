"""Pins build_host_binary: get a HOST-built tool (e.g. protoc) while cross-compiling by cheap-checking the
host build dir, then bootstrapping via a `mama <host> build` child on a miss - plus host_platform_name/host_build_dir."""
import os, sys, pytest
from unittest.mock import patch

from testutils import executable_extension, make_configured_target, set_mock_platform, touch_file as _touch
from mama import build_config as bc
from mama.utils.paths import path_join
from mama import build_target as bt
from mama.platforms.android import Android
from mama.platforms.linux import Linux
from mama.platforms.macos import Macos
from mama.platforms.windows import Windows

# build_host_binary names a host executable, so the host suffix belongs in the path a test writes
PROTOC = f'bin/protoc{executable_extension()}'


@pytest.fixture(autouse=True)
def an_x64_host():
    """Every path here is an x64 host path, so an arm64 CI runner must not take another branch."""
    with patch.object(bc.System, 'aarch64', False), patch.object(bc.System, 'x86_64', True):
        yield


def _cross_target(tmp_path, name='android', host='linux', platform=Android, **cfg):
    """A target cross-compiling for `name` with host `host`, so host_build_dir() is a distinct sibling."""
    t, dep = make_configured_target(tmp_path, **cfg)
    set_mock_platform(dep.config, platform)
    dep.dep_dir = f'{tmp_path}/packages/libfoo'.replace('\\', '/')
    dep.build_dir = f'{dep.dep_dir}/{name}'
    dep.config.host_platform_name.return_value = host
    dep.config.root_source_dir = str(tmp_path)
    return t, dep


def _sibling(dep, dir_name, relpath=None):
    """A path inside another build dir of the same dep."""
    return path_join(dep.dep_dir, dir_name, relpath or PROTOC)


def _hit(t, expected):
    """The tool answers from disk, so no child runs."""
    with patch('mama.build_target.SubProcess.run') as run:
        assert t.build_host_binary('bin/protoc') == expected
        run.assert_not_called()


def _miss(t):
    """Nothing on disk answers, so the bootstrap child runs once and finds nothing either."""
    with patch('mama.build_target.SubProcess.run', return_value=1) as run:
        assert t.build_host_binary('bin/protoc') is None
        run.assert_called_once()


def _bootstrapped(t, expected):
    """The predicted dir answers nothing, so the child runs, and its own dir answers afterwards."""
    with patch('mama.build_target.SubProcess.run', return_value=0) as run:
        assert t.build_host_binary('bin/protoc') == expected
        run.assert_called_once()


# -- host_platform_name -------------------------------------------------------

@pytest.mark.parametrize('windows,macos,expected', [
    (True, False, 'windows'), (False, True, 'macos'), (False, False, 'linux'),
])
def test_host_platform_name_follows_the_host_os(windows, macos, expected):
    cfg = bc.BuildConfig.__new__(bc.BuildConfig)  # bypass __init__: the method reads only System
    with patch.object(bc.System, 'windows', windows), patch.object(bc.System, 'macos', macos):
        assert cfg.host_platform_name() == expected


# -- host_build_dir -----------------------------------------------------------

def test_host_build_dir_is_a_sibling_named_after_the_host(tmp_path):
    t, dep = _cross_target(tmp_path)  # build_dir=.../libfoo/android, host=linux
    assert t.host_build_dir() == os.path.dirname(dep.build_dir).replace('\\', '/') + '/linux'
    assert t.host_build_dir('bin/protoc').endswith('/libfoo/linux/bin/protoc')


def test_the_host_dir_names_the_compiler_the_child_will_use(tmp_path):
    t, _ = _cross_target(tmp_path, clang=True, gcc=False)  # whatever this run resolved, the child repeats
    assert t.host_build_dir().endswith('/linux-clang')


def test_the_host_dir_names_the_dep_args(tmp_path):
    t, dep = _cross_target(tmp_path)
    dep.target_args = ['LGPL']  # an arg of add_git(), so the child's graph carries it too
    assert t.host_build_dir().endswith('/linux-lgpl')


def test_the_host_dir_keeps_the_sanitizer_and_the_coverage_out(tmp_path):
    # the child gets neither flag, so a host tool is never built under one
    t, dep = _cross_target(tmp_path, sanitize='address')
    dep.config.coverage = True
    assert t.host_build_dir().endswith('/linux')


def test_the_host_dir_follows_the_host_arch(tmp_path):
    t, _ = _cross_target(tmp_path)
    with patch.object(bc.System, 'aarch64', True), patch.object(bc.System, 'x86_64', False):
        assert t.host_build_dir().endswith('/linuxarm')


# -- build_host_binary --------------------------------------------------------

def test_native_build_returns_the_local_binary_without_a_child(tmp_path):
    t, dep = make_configured_target(tmp_path)  # build_dir ends in 'linux'
    dep.config.name.return_value = 'linux'
    dep.config.host_platform_name.return_value = 'linux'
    _hit(t, _touch(t.build_dir(PROTOC)))


def test_a_32_bit_build_of_a_64_bit_host_is_still_the_host(tmp_path):
    # the host runs the x86 tool it just built, so a second host build would be waste
    t, _ = _cross_target(tmp_path, name='linux32', platform=Linux, arch='x86')
    _hit(t, _touch(t.build_dir(PROTOC)))


def test_a_build_for_an_arch_the_host_cannot_run_is_not_the_host(tmp_path):
    t, _ = _cross_target(tmp_path, name='linuxarm', platform=Linux, arch='arm64')
    _touch(t.build_dir(PROTOC))  # an arm64 tool, which this x64 host cannot execute
    _miss(t)


def test_cross_hit_returns_the_host_binary_without_a_child(tmp_path):
    t, dep = _cross_target(tmp_path)
    _hit(t, _touch(t.host_build_dir(PROTOC)))  # cheap check hit


def test_cross_miss_bootstraps_then_returns_the_produced_binary(tmp_path):
    t, dep = _cross_target(tmp_path)
    produced = t.host_build_dir(PROTOC)
    def fake_child(argv, cwd=None, **kw):
        _touch(produced); return 0   # the `mama <host> build` child produces protoc
    with patch('mama.build_target.SubProcess.run', side_effect=fake_child) as run:
        assert t.build_host_binary('bin/protoc') == produced
    argv = run.call_args.args[0]
    assert argv[0] == sys.executable and argv[-4:] == ['linux', 'build', 'target=libfoo', 'arch=x64']
    assert run.call_args.kwargs['cwd'] == str(tmp_path)   # root project, so the child resolves the graph


@pytest.mark.parametrize('clang, expect', [(False, 'gcc'), (True, 'clang')])
def test_a_command_line_compiler_reaches_the_child_on_a_linux_host(tmp_path, clang, expect):
    # the linux build dir names the compiler, and this flag beats the mamafile preference in the child
    t, _ = _cross_target(tmp_path, clang=clang, gcc=not clang, compiler_from_args=True)
    with patch('mama.build_target.SubProcess.run', return_value=1) as run:
        t.build_host_binary('bin/protoc')
    assert run.call_args.args[0][-1] == expect


def test_a_mamafile_compiler_preference_never_reaches_the_child(tmp_path):
    # the root settings() lock sets compiler_cmd on every run, so only compiler_from_args names a choice
    t, _ = _cross_target(tmp_path, clang=True, gcc=False, compiler_cmd=True)
    with patch('mama.build_target.SubProcess.run', return_value=1) as run:
        t.build_host_binary('bin/protoc')
    assert run.call_args.args[0][-1] == 'arch=x64'


def test_an_intel_mac_names_its_own_arch_and_not_the_platform_default(tmp_path):
    # macOS defaults to arm64, and an Intel Mac cannot run an arm64 tool
    t, _ = _cross_target(tmp_path, host='macos')
    with patch('mama.build_target.SubProcess.run', return_value=1) as run:
        assert t.host_build_dir().endswith('/macos')
        t.build_host_binary('bin/protoc')
    assert run.call_args.args[0][-1] == 'arch=x64'


def test_the_child_gets_no_compiler_on_a_windows_host(tmp_path):
    # `gcc` or `clang` deselects MSVC in the child, and a windows build dir names no compiler
    t, _ = _cross_target(tmp_path, host='windows')
    with patch('mama.build_target.SubProcess.run', return_value=1) as run:
        t.build_host_binary('bin/protoc')
    assert run.call_args.args[0][-4:] == ['windows', 'build', 'target=libfoo', 'arch=x64']


# -- the search over host build dirs -------------------------------------------

def test_a_host_dir_the_child_named_differently_answers_after_the_bootstrap(tmp_path):
    # the child resolves its own dep args, so it can write a dir this process did not predict
    t, dep = _cross_target(tmp_path)
    dep.target_args = ['LGPL']  # predicts linux-lgpl
    _bootstrapped(t, _touch(_sibling(dep, 'linux-clang')))


def test_another_variant_never_answers_before_the_bootstrap(tmp_path):
    # a dep arg changes what a tool does, so a warm linux-lgpl must not serve a run that asked for linux
    t, dep = _cross_target(tmp_path)
    _touch(_sibling(dep, 'linux-lgpl'))
    with patch('mama.build_target.SubProcess.run', return_value=1) as run:
        assert t.build_host_binary('bin/protoc') is None
        run.assert_called_once()  # the child ran, rather than the neighbour answering


@pytest.mark.parametrize('dirs', [
    ['linux-asan'], ['linux-cov'], ['linux-clang-cov-lgpl'],  # instrumented objects, never a build tool
    ['linux32', 'linuxarm'],                                  # another arch, which this host cannot run
    ['linux-headers'],                                        # the source dir of a dep, not a build dir
])
def test_the_search_refuses_every_dir_that_is_not_a_host_build(tmp_path, dirs):
    t, dep = _cross_target(tmp_path)
    if dirs == ['linux-headers']: dep.src_dir = path_join(dep.dep_dir, 'linux-headers')
    for name in dirs: _touch(_sibling(dep, name))
    _miss(t)


@pytest.mark.parametrize('platform, host, arch, runs', [
    (Linux,   'x64',   'x86',   True),    # multilib
    (Linux,   'x64',   'arm64', False),
    (Linux,   'arm64', 'x64',   False),
    (Macos,   'arm64', 'x64',   True),    # Rosetta 2
    (Macos,   'x64',   'arm64', False),
    (Windows, 'arm64', 'x86',   True),    # the arm64 emulator takes both
    (Windows, 'x86',   'x64',   False),
])
def test_what_each_host_can_run(platform, host, arch, runs):
    with patch('mama.platforms.platform.host_arch', return_value=host), \
         patch('mama.platforms.macos.host_arch', return_value=host), \
         patch('mama.platforms.macos.rosetta_installed', return_value=True):
        assert platform(None).runs_on_host(arch) is runs


def test_apple_silicon_without_rosetta_runs_no_x64_tool(tmp_path):
    # Rosetta is an optional install, and the x64 tool cannot run without it
    with patch('mama.platforms.macos.host_arch', return_value='arm64'), \
         patch('mama.platforms.macos.rosetta_installed', return_value=False):
        assert Macos(None).runs_on_host('x64') is False


def test_the_search_takes_the_newest_host_tool(tmp_path):
    t, dep = _cross_target(tmp_path)
    dep.target_args = ['LGPL']  # predicts linux-lgpl, which nothing wrote
    os.utime(_touch(_sibling(dep, 'linux')), (1, 1))
    _bootstrapped(t, _touch(_sibling(dep, 'linux-clang')))


def test_the_predicted_dir_answers_before_any_other(tmp_path):
    # it names the compiler and the dep args of this run, so it holds the right variant
    t, dep = _cross_target(tmp_path)
    exact = _touch(t.host_build_dir(PROTOC))
    os.utime(exact, (1, 1))  # older than the neighbour, and still the answer
    _touch(_sibling(dep, 'linux-clang'))
    _hit(t, exact)


def test_a_dep_arg_that_spells_a_sanitizer_still_finds_its_tool(tmp_path):
    # `args=['ASAN']` names linux-asan with no instrumentation in it, and the predicted dir answers first
    t, dep = _cross_target(tmp_path)
    dep.target_args = ['ASAN']
    _hit(t, _touch(t.host_build_dir(PROTOC)))


def test_a_dep_arg_that_spells_a_sanitizer_finds_the_tool_its_child_wrote(tmp_path):
    # the widened search refuses a linux-asan dir, so the predicted path has to answer after the child
    t, dep = _cross_target(tmp_path)
    dep.target_args = ['ASAN']
    produced = t.host_build_dir(PROTOC)
    def child(argv, cwd=None, **kw): return _touch(produced) and 0
    with patch('mama.build_target.SubProcess.run', side_effect=child) as run:
        assert t.build_host_binary('bin/protoc') == produced
        run.assert_called_once()


def test_bootstrap_captures_the_child_instead_of_letting_it_own_the_terminal(tmp_path):
    """An uncaptured child inherits stdout and draws its own live region over ours, tearing the
    parent's cursor math apart - every child line must come back through console()."""
    t, dep = _cross_target(tmp_path)
    with patch('mama.build_target.SubProcess.run', return_value=1) as run, \
         patch('mama.build_target.console') as console:
        t.build_host_binary('bin/protoc')
        run.call_args.kwargs['io_func'](None, '* build J4  protoc  bld 2.1s\n')
    assert console.call_args.args[0].endswith('| * build J4  protoc  bld 2.1s')


@pytest.mark.parametrize('status', [0, 1])  # exit 0 and no binary, or a failed child
def test_a_bootstrap_that_produced_nothing_returns_none(tmp_path, status):
    t, _ = _cross_target(tmp_path)
    with patch('mama.build_target.SubProcess.run', return_value=status):
        assert t.build_host_binary('bin/protoc') is None


def test_auto_build_false_never_spawns_a_child(tmp_path):
    t, dep = _cross_target(tmp_path)
    with patch('mama.build_target.SubProcess.run') as run:
        assert t.build_host_binary('bin/protoc', auto_build=False) is None
        run.assert_not_called()


def test_windows_host_resolves_the_exe_suffix(tmp_path):
    t, dep = _cross_target(tmp_path)
    binary = _touch(t.host_build_dir('bin/protoc.exe'))
    with patch.object(bt.System, 'windows', True):
        _hit(t, binary)   # 'bin/protoc' -> 'bin/protoc.exe'
