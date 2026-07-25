"""Pins the cross-process dep-dir lock: an flock/msvcrt guard that serialises a dep's shim+checkout across
concurrent mama processes (so a reclone-wipe can't rmtree a dir another builder is shimming into), never hangs."""
import contextlib, os, pytest
from unittest.mock import patch

from testutils import make_mock_dep
from mama.utils.dir_lock import interprocess_dir_lock
from mama import build_dependency as bd


# ── the lock primitive ───────────────────────────────────────────────────────

def test_acquires_uncontended_and_puts_the_sidecar_beside_not_inside(tmp_path):
    d = str(tmp_path / 'pkg' / 'libfoo')
    with interprocess_dir_lock(d, timeout=5) as ok:
        assert ok is True
        # beside libfoo, so a reclone-wipe rmtree of libfoo can't unlink a held lock and break exclusion
        assert os.path.exists(str(tmp_path / 'pkg' / '.libfoo.mama.lock'))
        assert not os.path.exists(os.path.join(d, '.mama.lock'))


def test_second_acquirer_times_out_but_still_runs_then_frees(tmp_path):
    d = str(tmp_path / 'dep')
    with interprocess_dir_lock(d, timeout=5) as first:
        assert first is True
        # a distinct fd on the same file conflicts even within one process (flock is per-open-file) -> the
        # nested acquire times out, but its block still runs (best-effort, never blocks the build forever)
        with interprocess_dir_lock(d, timeout=0.3) as second:
            assert second is False
    with interprocess_dir_lock(d, timeout=5) as again:
        assert again is True   # released on exit -> acquirable again


def test_lock_is_released_even_when_the_body_raises(tmp_path):
    d = str(tmp_path / 'dep')
    with pytest.raises(RuntimeError):
        with interprocess_dir_lock(d, timeout=5) as ok:
            assert ok is True
            raise RuntimeError('boom')
    with interprocess_dir_lock(d, timeout=5) as again:
        assert again is True   # finally-released despite the exception


# ── wiring into BuildDependency._load ────────────────────────────────────────

def test_git_load_runs_shim_and_checkout_inside_the_dep_dir_lock(tmp_path):
    dep = make_mock_dep(tmp_path)   # a git dep, is_root=False
    seq = []

    @contextlib.contextmanager
    def spy_lock(lock_dir, timeout):
        seq.append(('enter', lock_dir))
        try: yield True
        finally: seq.append('exit')

    with patch('mama.build_dependency.interprocess_dir_lock', spy_lock), \
         patch.object(dep, '_try_artifactory_shim', lambda: seq.append('shim') or False), \
         patch.object(dep, '_git_checkout_if_needed', lambda: seq.append('checkout') or False), \
         patch.object(dep, '_load_target', side_effect=RuntimeError('stop')):  # halt right after the locked region
        with pytest.raises(RuntimeError, match='stop'):
            dep._load()

    labels = [e[0] if isinstance(e, tuple) else e for e in seq]
    assert labels == ['enter', 'shim', 'checkout', 'exit']   # both under the lock; released before the parse
    assert seq[0][1] == dep.dep_dir                            # keyed on the shared dep_dir
