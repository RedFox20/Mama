"""Pins `mama clean` / `clean all`: cleans during load, then stops - no configure/build/package after."""
from unittest.mock import Mock, patch
from mama.main import mamabuild


def _run(args, tmp_path):
    (tmp_path / 'CMakeLists.txt').write_text('project(dummy)\n')  # mamabuild refuses a dir with neither file
    with patch('mama.main.load_dependency_chain') as load, \
         patch('mama.main.execute_task_chain') as serial, \
         patch('mama.main.execute_task_chain_parallel') as parallel, \
         patch('mama.main.execute_unified') as unified, \
         patch('mama.main._init_platform_compilers'):
        mamabuild(args, source_dir=str(tmp_path))
    return load.called, (serial.called or parallel.called or unified.called)


def test_clean_all_stops_after_cleaning(tmp_path):
    loaded, executed = _run(['clean', 'all'], tmp_path)
    assert loaded and not executed  # package() over a wiped dir dies in mamafile asserts


def test_plain_clean_stops_after_cleaning(tmp_path):
    assert not _run(['clean'], tmp_path)[1]


def test_rebuild_still_runs_the_chain(tmp_path):
    assert _run(['rebuild', 'all'], tmp_path)[1]  # rebuild = clean + build, the build half must survive


def test_build_still_runs_the_chain(tmp_path):
    assert _run(['build', 'all'], tmp_path)[1]


def _git_dep(tmp_path, **cfg):
    """A git dep with no source on disk - the shape a previous clean leaves behind (its shim marker
    lived in the build dir that clean deleted)."""
    from testutils import make_mock_dep
    dep = make_mock_dep(tmp_path, **cfg)
    dep.config.clean_only.return_value = cfg.get('clean', False) and not cfg.get('build', False)
    return dep


def _fetched_during_load(dep):
    """Did loading this dep reach out to git? Returns the checkout call count."""
    def load_target(self):
        self.target = Mock(args='', build_products=[], name=self.name)
        return self.target
    with patch.object(type(dep), '_git_checkout_if_needed', return_value=False) as checkout, \
         patch.object(type(dep), '_try_artifactory_shim', return_value=False), \
         patch.object(type(dep), '_try_artifactory_load', return_value=False), \
         patch.object(type(dep), '_load_target', load_target), \
         patch.object(type(dep), 'clean'):
        dep._load()
    return checkout.call_count


def test_clean_does_not_clone_missing_sources(tmp_path):
    # `mama clean all` used to spend minutes cloning deps whose shim marker an earlier clean removed,
    # only to delete the directory it just filled.
    assert _fetched_during_load(_git_dep(tmp_path, clean=True, build=False)) == 0


def test_a_build_still_fetches(tmp_path):
    assert _fetched_during_load(_git_dep(tmp_path, clean=False, build=True)) == 1
    assert _fetched_during_load(_git_dep(tmp_path, clean=True, build=True)) == 1   # rebuild = clean + build
