import os
import sys
import pytest

# tests/ for `import testutils`, the project root for `from mama.x import y`, so no test file repeats the sys.path setup.
_here = os.path.dirname(__file__)
_repo_root = os.path.abspath(os.path.join(_here, '..'))
sys.path.insert(0, _here)
sys.path.insert(0, _repo_root)


from testutils import (has_case_sensitive_fs, is_linux, make_example_remote,  # after the sys.path setup above
                       set_repo_template_dir)


def pytest_runtest_setup(item):
    """Skip a test whose host cannot run it. MIPS, every Yocto SDK and install-raspi need a Linux host, so
    the code under test raises elsewhere. A case_sensitive_fs test deploys two dirs whose names differ only
    by case, and Windows and macOS hold one dir for that pair."""
    if item.get_closest_marker('linux_host') and not is_linux():
        pytest.skip('needs a Linux host')
    if item.get_closest_marker('case_sensitive_fs') and not has_case_sensitive_fs():
        pytest.skip('needs a case-sensitive filesystem')


@pytest.fixture(scope='session', autouse=True)
def _own_cache_dir(tmp_path_factory):
    """Give the suite one shared compiler seed, in a cache dir of its own. The seed then costs one cmake
    probe for the whole suite instead of one per test. A build defaults to a workspace seed, so the flag
    has to say so. MAMA_CACHE_DIR keeps every test out of the cache of the developer."""
    os.environ['MAMA_CACHE_DIR'] = str(tmp_path_factory.mktemp('mama_cache'))
    os.environ['MAMA_GLOBAL_COMPILER_CACHE'] = '1'


@pytest.fixture(scope='session', autouse=True)
def _git_identity():
    """Name the git author and committer once for the session. A test repo would otherwise spend two
    `git config` spawns of its own. One git spawn costs about 27 milliseconds on Windows."""
    for field, value in (('NAME', 'mama test'), ('EMAIL', 'test@mama')):
        os.environ[f'GIT_AUTHOR_{field}'] = os.environ[f'GIT_COMMITTER_{field}'] = value


@pytest.fixture(scope='session', autouse=True)
def _repo_template_dir(tmp_path_factory):
    """Give the repo helper a session-lifetime dir, so it builds each repo shape with git one time and
    copies it after that. pytest removes the dir at the end of the session."""
    set_repo_template_dir(str(tmp_path_factory.mktemp('repo_templates')))


@pytest.fixture(scope='session')
def buildable_example_remote(tmp_path_factory):
    """The same remote, but with no mamafile, so mama really configures and builds the clone. Only a test
    that links the library wants this. It publishes its own variables, so the order of the two fixtures
    within a session cannot matter."""
    info = make_example_remote(tmp_path_factory.mktemp('buildable_remote'), buildable=True)
    os.environ['MAMA_TEST_BUILD_REMOTE_URL'] = info['url']
    os.environ['MAMA_TEST_BUILD_REMOTE_OLD'] = info['old']
    return info


@pytest.fixture(scope='session')
def example_remote(tmp_path_factory):
    """Publish the local example remote through the environment, so each test mamafile names no url of its
    own. Built once per session. Before this, every git integration test cloned github, which cost about
    20 seconds per run and failed whenever the network did.
    Returns {url, old, new}. `old` is the commit without REMOTE_VERSION, `new` is the one with it."""
    info = make_example_remote(tmp_path_factory.mktemp('example_remote'))
    os.environ['MAMA_TEST_REMOTE_URL'] = info['url']
    os.environ['MAMA_TEST_REMOTE_OLD'] = info['old']
    os.environ['MAMA_TEST_REMOTE_NEW'] = info['new']
    return info


@pytest.fixture(autouse=True)
def _restore_cwd():
    """Restore the working directory after every test. The integration tests chdir into their own
    project copy, and the session removes that copy later."""
    cwd = os.getcwd()
    yield
    os.chdir(cwd)


@pytest.fixture(autouse=True)
def _forget_repo_status():
    """Drop the shared `git status` after every test. mama loads it once per run and every local dep
    reads it, so one test that loads it would answer for the working tree of the next."""
    yield
    from mama.utils import git_status as util
    util.forget_repo_status()


@pytest.fixture(autouse=True)
def _close_run_log():
    """Close the build log after every test that opened one. It holds a file under that test's tmp dir,
    and console() would keep writing into it for the whole session."""
    yield
    from mama.utils import log_writer, system
    log_writer.close_build_log()
    system.set_run_log(None)


@pytest.fixture(autouse=True)
def _unpace_connections():
    """Turn the connection pacer off after every test. It is process-global, and a test that drives the
    real retry path arms it. Every later git test would then sleep a quarter second per network command."""
    yield
    from mama.utils import ssh_multiplex
    ssh_multiplex.reset_connection_pacing()


@pytest.fixture(autouse=True)
def _disarm_abort():
    """Clear the process-wide abort flag after every test. A test that sets the flag and then fails
    leaves it set. Every later test that spawns a subprocess then dies on that stale flag."""
    yield
    from mama.utils import abort
    abort.clear()


@pytest.fixture
def interactive_terminal(monkeypatch):
    """Pretend stdout is a terminal. pytest captures stdout, so is_headless() reads True by default and
    every progress redraw would throttle away."""
    from mama.utils import system
    monkeypatch.setattr(system, 'is_headless', lambda: False)


@pytest.fixture
def no_cmake_writes(monkeypatch):
    """Silence the two disk writes every execute_unified run does. The scheduler fakes have no build
    dir, so mama.cmake and c_cpp_properties.json have nowhere to go."""
    from mama import dependency_chain as dc
    monkeypatch.setattr(dc, '_save_mama_cmake_and_dependencies_cmake', lambda d: None)
    monkeypatch.setattr(dc, '_save_vscode_compile_commands', lambda d: None)


def pytest_configure(config):
    # Temproot, not basetemp: pytest makes a locked pytest-<N> dir per run under it. A wiped basetemp kills a concurrent run.
    if not config.option.basetemp:
        temproot = os.path.join(_repo_root, '.pytest_tmp')
        os.makedirs(temproot, exist_ok=True)
        os.environ.setdefault('PYTEST_DEBUG_TEMPROOT', temproot)
