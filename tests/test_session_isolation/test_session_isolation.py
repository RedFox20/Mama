"""Pins what lets two pytest sessions run at the same time. Each session gets its own tmp root, and an
integration test builds in a private copy of its project, not in its own source directory."""
import os, re

from testutils import init


def test_the_tmp_root_is_per_session_inside_the_repo(tmp_path):
    root = os.environ['PYTEST_DEBUG_TEMPROOT'].replace('\\', '/')
    assert root.endswith('/.pytest_tmp')  # gitignored repo subtree, not system temp
    # .pytest_tmp/pytest-of-<user>/pytest-<N>/[popen-gw<N>/]<test name><i>. Each session gets its own
    # numbered dir, an xdist worker adds one level below it, and a fixed basetemp gets no number at all.
    assert any(re.fullmatch(r'pytest-\d+', p.name) for p in tmp_path.parents)
    assert '.pytest_tmp' in str(tmp_path).replace('\\', '/')


def test_a_test_project_is_copied_without_the_generated_dirs(tmp_path):
    src, work = tmp_path / 'src', tmp_path / 'work'
    (src / 'packages' / 'ExampleRemote').mkdir(parents=True)
    (src / 'mamafile.py').write_text('# project\n')
    work.mkdir()
    project = init(str(src / 'test_x.py'), work)
    assert os.getcwd() == project and project.startswith(str(work))  # mama builds in the copy
    assert os.path.exists('mamafile.py')
    assert not os.path.exists('packages')  # a stale clone from an earlier run never reaches the copy
