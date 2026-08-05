"""Pins how a failing package() hook reports: named and fatal, except on a list run."""
from unittest.mock import patch
import pytest

from testutils import make_mock_dep

from mama.build_target import BuildTarget
from mama.utils.errors import BuildError


def _target(tmp_path, **config):
    """A target whose package() raises, the way a mamafile asserts on a missing build product."""
    dep = make_mock_dep(tmp_path, **config)
    def package(self): raise RuntimeError('libfoo.so not found at /pkg/lib')
    cls = type('mamafile', (BuildTarget,), {'package': package})
    return cls(name='libfoo', config=dep.config, dep=dep, args=[])


def test_a_failing_package_names_the_target(tmp_path):
    # a bare traceback buries the target name under mama's own call stack
    target = _target(tmp_path, list=False)
    with pytest.raises(BuildError, match='Package failed for target libfoo: libfoo.so not found'):
        target._run_package_hook()


def test_a_list_run_reports_the_gap_and_carries_on(tmp_path):
    target = _target(tmp_path, list=True, print=True)
    with patch('mama.build_target.warning') as warn:
        target._run_package_hook()   # a list builds nothing, so it must not fail on a missing product
    assert 'INCOMPLETE' in warn.call_args[0][0] and 'libfoo.so not found' in warn.call_args[0][0]
