"""Pins the run tasks: a target without a test() reports a skip, and a refused test still starts."""
from unittest.mock import patch

from testutils import make_mock_dep

from mama.build_target import BuildTarget


def _target(tmp_path, hooks=(), **config):
    """A BuildTarget subclass that defines only `hooks`, the way a mamafile overrides them."""
    config = {'test': ' ', 'test_until_failure': 0, 'start': None, **config}
    dep = make_mock_dep(tmp_path, **config)
    dep.config.target_matches.return_value = True
    body = {h: (lambda self, args='': self.calls.append(h)) for h in hooks}
    cls = type('mamafile', (BuildTarget,), {**body, 'calls': []})
    return cls(name='libfoo', config=dep.config, dep=dep, args=[])


def test_a_mamafile_without_a_test_hook_reports_a_skip(tmp_path):
    target = _target(tmp_path, print=True)
    with patch('mama.build_target.warning') as warn:
        target._execute_run_tasks()
    assert 'SKIPPED' in warn.call_args[0][0] and target.calls == []


def test_a_mamafile_with_a_test_hook_runs_it(tmp_path):
    target = _target(tmp_path, hooks=['test'])
    target._execute_run_tasks()
    assert target.calls == ['test']


def test_a_refused_test_still_lets_start_ask_for_itself(tmp_path):
    # a shim refuses both, but the test refusal must not return before `start` gets its own check
    target = _target(tmp_path, hooks=['test', 'start'], start='run')
    with patch.object(BuildTarget, '_require_source', return_value=False) as require:
        target._execute_run_tasks()
    assert [c.args[0] for c in require.call_args_list] == ['test', 'start']


def test_overrides_reads_the_hook_the_mamafile_defines(tmp_path):
    assert _target(tmp_path, hooks=['build'])._has_custom_build()
    assert not _target(tmp_path)._has_custom_build()
