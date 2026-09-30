"""Pins the wasm platform: the SDK search, the flags every object shares, the generator and node."""
import os
from unittest.mock import patch
import pytest

from testutils import emsdk_node, executable_extension, make_emsdk_tree, make_stub_target, platform_config, platform_target, \
                      touch_file
from mama.buildsys.cmake import configure as cc
from mama.platforms.wasm import Wasm
from mama.utils import gdb, gtest, run
from mama.utils.paths import quoted


@pytest.fixture(autouse=True)
def _no_EMSDK_NODE(monkeypatch): monkeypatch.delenv('EMSDK_NODE', raising=False)


def _stub(tmp_path, **config):
    return make_stub_target(tmp_path, Wasm, print=False, arch='wasm32', **config)


def _wasm(tmp_path, **config) -> Wasm:
    return _stub(tmp_path, **config).config.platform


# --- the SDK search ---

@pytest.mark.parametrize('subdir', ['', '/upstream/emscripten'])
def test_an_explicit_sdk_is_the_emsdk_root_or_its_emscripten_dir(subdir, tmp_path, fake_toolchains):
    wasm = _wasm(tmp_path)
    wasm.init_toolchain(fake_toolchains['emsdk'] + subdir)
    assert (wasm.emsdk, wasm.emscripten) == (fake_toolchains['emsdk'], f'{fake_toolchains["emsdk"]}/upstream/emscripten')


def test_a_toolchain_file_gets_a_warning_and_the_sdk_file_wins(tmp_path, fake_toolchains):
    wasm = _wasm(tmp_path)
    with patch('mama.platforms.wasm.warning', autospec=True) as warn:
        wasm.init_toolchain(fake_toolchains['emsdk'], toolchain_file='my.cmake')
    assert 'my.cmake' in warn.call_args.args[0] and wasm.toolchain().toolchain_file.endswith('/Emscripten.cmake')


def test_an_explicit_dir_with_no_sdk_raises(tmp_path):
    with pytest.raises(RuntimeError, match='No Emscripten SDK at'):
        _wasm(tmp_path).init_toolchain(str(tmp_path))


def _on_path(monkeypatch, path):
    monkeypatch.setattr('mama.platforms.wasm.find_executable_from_system', lambda name, follow_symlinks=False: path)


@pytest.fixture
def no_EMSDK(tmp_path, monkeypatch):
    """No EMSDK and an empty home, so only the emcc on PATH can name the SDK."""
    monkeypatch.delenv('EMSDK', raising=False)
    for home in ('HOME', 'USERPROFILE'): monkeypatch.setenv(home, str(tmp_path / 'home'))
    return lambda path: _on_path(monkeypatch, path)


def test_without_EMSDK_the_emcc_on_path_names_the_sdk(tmp_path, fake_toolchains, no_EMSDK):
    no_EMSDK(f'{fake_toolchains["emsdk"]}/upstream/emscripten/emcc')
    wasm = _wasm(tmp_path)
    wasm.init_default()
    assert wasm.emsdk == fake_toolchains['emsdk']


def test_no_sdk_anywhere_raises(tmp_path, no_EMSDK):
    no_EMSDK('')
    with pytest.raises(EnvironmentError, match='Set env EMSDK'):
        _wasm(tmp_path).init_default()


def test_a_standalone_emscripten_dir_has_no_emsdk_and_takes_the_node_on_path(tmp_path, monkeypatch):
    root = make_emsdk_tree(tmp_path.as_posix() + '/emsdk')
    os.remove(f'{root}/emsdk.py')
    _on_path(monkeypatch, '/usr/bin/node')
    wasm = _wasm(tmp_path)
    wasm.init_toolchain(f'{root}/upstream/emscripten')
    assert wasm.emsdk == '' and wasm.launcher() == '/usr/bin/node' and wasm.gcov_command() == ''


def test_an_older_sdk_on_windows_has_bat_tools(tmp_path, monkeypatch):
    emscripten = tmp_path.as_posix() + '/emscripten'
    touch_file(f'{emscripten}/cmake/Modules/Platform/Emscripten.cmake')
    monkeypatch.setattr('mama.platforms.wasm.System.windows', True)
    wasm = _wasm(tmp_path)
    wasm.init_toolchain(emscripten)
    assert wasm.archiver() == f'{emscripten}/emar.bat'


def test_the_toolchain_is_the_one_the_sdk_ships(tmp_path, fake_toolchains):
    wasm = _wasm(tmp_path)
    tc = wasm.toolchain()
    emscripten, ext = f'{fake_toolchains["emsdk"]}/upstream/emscripten', executable_extension()
    assert (tc.cc, tc.cxx) == (f'{emscripten}/emcc{ext}', f'{emscripten}/em++{ext}')
    assert tc.toolchain_file == f'{emscripten}/cmake/Modules/Platform/Emscripten.cmake' and tc.toolchain_file_is_complete
    assert wasm.archiver() == f'{emscripten}/emar{ext}'


@pytest.mark.parametrize('emsdk_dir', ['emsdk', 'my emsdk'])
def test_the_coverage_report_reads_the_llvm_cov_of_the_emsdk_never_emcc(tmp_path, emsdk_dir):
    root = make_emsdk_tree(f'{tmp_path.as_posix()}/{emsdk_dir}')
    wasm = _wasm(tmp_path, gcc=True, cc_path=f'{root}/upstream/emscripten/emcc')
    wasm.init_toolchain(root)
    assert wasm.gcov_command() == quoted(f'{root}/upstream/bin/llvm-cov{executable_extension()}') + ' gcov'


def test_the_archive_names_the_sdk_version():
    config = platform_config(Wasm)
    paths = ('/emsdk/emcc', '/emsdk/em++', '4.0.10')
    with patch.object(type(config), 'get_preferred_compiler_paths', autospec=True, return_value=paths):
        assert config.platform.compiler_version_tag() == 'emcc4.0'


@pytest.mark.parametrize('env, expected', [({}, 'mama'), ({'EMSDK_PYTHON': '/py'}, '/py')])
def test_an_em_tool_runs_on_the_python_of_mama_unless_the_env_names_one(env, expected, tmp_path, fake_toolchains, monkeypatch):
    monkeypatch.delenv('EMSDK_PYTHON', raising=False)
    monkeypatch.setattr('mama.platforms.wasm.sys.executable', 'mama')
    with patch.dict(os.environ, env):
        _wasm(tmp_path).archiver()
        assert os.environ['EMSDK_PYTHON'] == expected


# --- the flags every object shares ---

def test_every_object_compiles_with_wasm_exceptions_and_without_pic_or_march(tmp_path, fake_toolchains):
    t, _ = platform_target(tmp_path, Wasm)
    opts = cc._default_options(t)
    assert '-fwasm-exceptions' in t.cmake_cxxflags and '-fwasm-exceptions' in t.cmake_ldflags
    assert '-march' not in t.cmake_cxxflags and 'CMAKE_POSITION_INDEPENDENT_CODE=ON' not in opts


def test_a_target_without_exceptions_still_links_a_library_that_throws(tmp_path, fake_toolchains):
    t, _ = platform_target(tmp_path, Wasm)
    t.enable_exceptions = False
    cc._default_options(t)
    assert '-fno-exceptions' in t.cmake_cxxflags and '-fwasm-exceptions' not in t.cmake_cxxflags
    assert '-fwasm-exceptions' in t.cmake_ldflags


def test_a_sanitizer_adds_no_pie_and_coverage_adds_no_gcc_flag(tmp_path, fake_toolchains):
    """check_platform sets config.gcc on wasm, and emcc is clang."""
    t, _ = platform_target(tmp_path, Wasm, sanitize='address', coverage='default', user_target='libfoo')
    opts = cc._default_options(t)
    assert '-fsanitize' in t.cmake_cxxflags and '--coverage' in t.cmake_cxxflags
    assert '-fPIE' not in t.cmake_cxxflags and '-pie' not in t.cmake_ldflags and '-fprofile-abs-path' not in t.cmake_cxxflags
    assert cc._named_option(opts, 'CMAKE_EXE_LINKER_FLAGS') == '-fwasm-exceptions -fsanitize=address --coverage'


def test_wasm_builds_with_ninja_whatever_the_generator_choice():
    """The SDK ships no make program, so the default generator of a Windows host cannot build it."""
    config = platform_config(Wasm, ninja_path='/usr/bin/ninja', prefer_ninja=False)
    assert config.prefers_ninja_build()
    config.ninja_path = ''
    with pytest.raises(EnvironmentError, match='no ninja executable'): config.prefers_ninja_build()


def test_a_target_that_disables_ninja_still_gets_ninja(tmp_path, fake_toolchains):
    t, _ = platform_target(tmp_path, Wasm, ninja_path='/usr/bin/ninja')
    t.disable_ninja_build()
    assert cc._generator(t) == '-G "Ninja"' and 'CMAKE_MAKE_PROGRAM="/usr/bin/ninja"' in cc._default_options(t)


# --- node runs a program this project built ---

@pytest.fixture
def no_which():
    """get_cwd_exe_args runs a program on PATH first, and the host PATH can hold a `tests`."""
    with patch('mama.utils.run.shutil.which', return_value=None): yield


def _command(tmp_path, command, built=True):
    target = _stub(tmp_path)
    _, exe, args = run.get_cwd_exe_args(target, command, root_dir=target.build_dir(), built=built)
    return run.command_line(target, exe, args, built)


def test_a_built_program_runs_its_js_under_the_node_of_the_sdk(tmp_path, fake_toolchains, no_which):
    assert _command(tmp_path, 'bin/tests --gtest_brief=1') == \
        f'{fake_toolchains["node"]} {tmp_path.as_posix()}/build/bin/tests.js --gtest_brief=1'


def test_the_newest_node_of_the_sdk_wins(tmp_path, monkeypatch):
    monkeypatch.setenv('EMSDK', make_emsdk_tree(tmp_path.as_posix() + '/emsdk', ('8.9.1', '22.16.0')))
    assert _wasm(tmp_path).launcher() == f'{tmp_path.as_posix()}/emsdk/{emsdk_node("22.16.0")}'


def test_EMSDK_NODE_wins_over_the_node_of_the_sdk(tmp_path, fake_toolchains, monkeypatch, no_which):
    monkeypatch.setenv('EMSDK_NODE', '/opt/node/bin/node')
    assert _command(tmp_path, 'bin/tests').startswith('/opt/node/bin/node ')


def test_the_node_search_runs_once(tmp_path, fake_toolchains, monkeypatch):
    wasm = _wasm(tmp_path)
    node = wasm.launcher()
    monkeypatch.setenv('EMSDK_NODE', '/opt/node/bin/node')
    assert wasm.launcher() == node


def test_no_node_anywhere_raises(tmp_path, monkeypatch):
    monkeypatch.setenv('EMSDK', make_emsdk_tree(tmp_path.as_posix() + '/emsdk', node_versions=()))
    _on_path(monkeypatch, '')
    with pytest.raises(EnvironmentError, match='No node found'):
        _wasm(tmp_path).launcher()


def test_a_host_tool_keeps_its_name_and_runs_without_node(tmp_path, fake_toolchains, no_which):
    assert _command(tmp_path, 'tools/protoc --version', built=False) == f'{tmp_path.as_posix()}/build/tools/protoc --version'


def test_the_gtest_helper_runs_the_test_program_under_node(tmp_path, fake_toolchains, no_which):
    ran = []
    with patch('mama.utils.run.execute_echo', autospec=True, side_effect=lambda cwd, cmd, **kw: ran.append(cmd)):
        gtest.run_gtest(_stub(tmp_path), 'bin/tests')
    assert ran[0].startswith(f'{fake_toolchains["node"]} ') and '/bin/tests.js ' in ran[0]


@pytest.mark.parametrize('project', ['', 'my project/'])  # get_cwd_exe_args quotes a path with a space
def test_the_debugger_helper_runs_the_test_program_under_node(tmp_path, fake_toolchains, no_which, project):
    touch_file(f'{tmp_path}/{project}src/bin/tests.js')
    target = _stub(tmp_path / project)
    target.msvc = False  # run_gdb reads it off the target
    with patch('mama.utils.gdb.execute_echo', autospec=True) as echo:
        gdb.run_gdb(target, 'bin/tests')
    cmd = echo.call_args.kwargs['cmd']
    assert cmd.startswith(f'{fake_toolchains["node"]} ') and '/bin/tests.js' in cmd
