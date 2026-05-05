"""Unit tests for mama.utils.ssh_multiplex pure-logic helpers.

These cover:
* URL -> (user, host, port) parsing for SSH and non-SSH URLs.
* ssh-G probe output -> options decision: ControlMaster/ControlPath added
  only when the user has not already configured multiplexing.
* GIT_SSH_COMMAND wrapper arg parsing.

Network-touching paths (probe, prewarm) are mocked.
"""
from __future__ import annotations

import os
import sys
from unittest import mock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))
from mama.utils import ssh_multiplex as sm  # noqa: E402


class TestParseSshEndpoint:
    def test_scp_form(self):
        assert sm.parse_ssh_endpoint('git@github.com:foo/bar.git') == ('git', 'github.com', None)

    def test_scp_form_user_other_than_git(self):
        assert sm.parse_ssh_endpoint('alice@host.example:proj.git') == ('alice', 'host.example', None)

    def test_scp_form_no_user(self):
        # Falls back to default 'git' user.
        assert sm.parse_ssh_endpoint('host.example:proj.git') == ('git', 'host.example', None)

    def test_ssh_url_with_port(self):
        assert sm.parse_ssh_endpoint('ssh://git@host:2222/foo/bar.git') == ('git', 'host', '2222')

    def test_ssh_url_no_user(self):
        assert sm.parse_ssh_endpoint('ssh://host/foo/bar.git') == ('git', 'host', None)

    def test_https_rejected(self):
        assert sm.parse_ssh_endpoint('https://github.com/foo/bar.git') is None

    def test_http_rejected(self):
        assert sm.parse_ssh_endpoint('http://github.com/foo/bar.git') is None

    def test_file_url_rejected(self):
        assert sm.parse_ssh_endpoint('file:///srv/repos/foo.git') is None

    def test_local_path_rejected(self):
        assert sm.parse_ssh_endpoint('/srv/repos/foo.git') is None

    def test_relative_path_rejected(self):
        # 'foo/bar.git' has no colon — not scp-style, no scheme.
        assert sm.parse_ssh_endpoint('foo/bar.git') is None

    def test_empty_url(self):
        assert sm.parse_ssh_endpoint('') is None
        assert sm.parse_ssh_endpoint(None) is None

    def test_windows_path_rejected(self):
        # Windows drive paths must NOT be treated as scp-form.
        assert sm.parse_ssh_endpoint('C:/foo/bar') is None
        assert sm.parse_ssh_endpoint('D:\\repos\\proj') is None

    def test_host_with_no_path_rejected(self):
        # `host:` with nothing after isn't a real git URL.
        assert sm.parse_ssh_endpoint('git@host:') is None

    def test_bracketed_ipv6_rejected(self):
        # git itself doesn't treat scp-form bracketed IPv6 as a URL.
        assert sm.parse_ssh_endpoint('git@[::1]:repo.git') is None


class TestIsMultiplexConfigured:
    def test_no_controlmaster_no_controlpath(self):
        assert not sm.is_multiplex_configured({'controlmaster': 'no', 'controlpath': 'none'})

    def test_controlmaster_auto_with_path(self):
        assert sm.is_multiplex_configured({'controlmaster': 'auto', 'controlpath': '~/.ssh/cm/%C'})

    def test_controlmaster_yes_with_path(self):
        assert sm.is_multiplex_configured({'controlmaster': 'yes', 'controlpath': '/tmp/sock'})

    def test_controlmaster_set_but_path_none(self):
        # ControlPath=none means no socket -> not multiplexed even with master=auto.
        assert not sm.is_multiplex_configured({'controlmaster': 'auto', 'controlpath': 'none'})

    def test_path_set_but_no_master(self):
        assert not sm.is_multiplex_configured({'controlmaster': 'no', 'controlpath': '/tmp/sock'})

    def test_empty_probe(self):
        # When ssh -G fails the probe is empty; treat as "not configured".
        assert not sm.is_multiplex_configured({})


class TestOptionsToAdd:
    def test_user_has_full_config(self):
        probe = {
            'controlmaster': 'auto',
            'controlpath': '~/.ssh/sockets/%C',
            'serveraliveinterval': '30',
            'serveralivecountmax': '5',
        }
        opts, we_own = sm.options_to_add(probe)
        assert opts == [], 'should add nothing when user has everything'
        assert we_own is False

    def test_user_has_nothing(self, tmp_path, monkeypatch):
        # Avoid mkdir on the user's actual ~/.ssh/cm.
        monkeypatch.setattr(sm, '_OUR_CONTROL_DIR', str(tmp_path / 'cm'))
        monkeypatch.setattr(sm, '_OUR_CONTROL_PATH', str(tmp_path / 'cm' / '%C'))
        probe = {'controlmaster': 'no', 'controlpath': 'none'}
        opts, we_own = sm.options_to_add(probe)
        assert we_own is True
        assert any(o.startswith('-oControlMaster=') for o in opts)
        assert any(o.startswith('-oControlPath=') for o in opts)
        assert any(o.startswith('-oControlPersist=') for o in opts)
        assert any(o.startswith('-oServerAliveInterval=') for o in opts)
        assert any(o.startswith('-oServerAliveCountMax=') for o in opts)

    def test_user_has_keepalives_only(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sm, '_OUR_CONTROL_DIR', str(tmp_path / 'cm'))
        monkeypatch.setattr(sm, '_OUR_CONTROL_PATH', str(tmp_path / 'cm' / '%C'))
        probe = {
            'controlmaster': 'no', 'controlpath': 'none',
            'serveraliveinterval': '60', 'serveralivecountmax': '3',
        }
        opts, we_own = sm.options_to_add(probe)
        assert we_own is True
        # We add multiplex but NOT keepalives (user has them already).
        assert any(o.startswith('-oControlMaster=') for o in opts)
        assert not any(o.startswith('-oServerAliveInterval=') for o in opts)
        assert not any(o.startswith('-oServerAliveCountMax=') for o in opts)

    def test_user_has_multiplex_only(self):
        probe = {
            'controlmaster': 'auto', 'controlpath': '/tmp/sock',
            'serveraliveinterval': '0',
        }
        opts, we_own = sm.options_to_add(probe)
        assert we_own is False
        # No control* options; only keepalives.
        assert not any(o.startswith('-oControlMaster=') for o in opts)
        assert not any(o.startswith('-oControlPath=') for o in opts)
        assert any(o.startswith('-oServerAliveInterval=') for o in opts)

    def test_windows_microsoft_ssh_skips_multiplex_keeps_keepalives(self, monkeypatch, tmp_path):
        # Microsoft OpenSSH on Windows has unreliable ControlMaster — the
        # master drops mid-session and leaves the socket file behind. We
        # detect it via the "for_Windows" banner string and skip multiplex.
        monkeypatch.setattr(sm, 'multiplex_known_broken', lambda: True)
        monkeypatch.setattr(sm, '_OUR_CONTROL_DIR', str(tmp_path / 'cm'))
        monkeypatch.setattr(sm, '_OUR_CONTROL_PATH', str(tmp_path / 'cm' / '%C'))
        probe = {'controlmaster': 'no', 'controlpath': 'none',
                 'serveraliveinterval': '0'}
        opts, we_own = sm.options_to_add(probe)
        assert we_own is False
        assert not any(o.startswith('-oControlMaster=') for o in opts)
        assert not any(o.startswith('-oControlPath=') for o in opts)
        assert not any(o.startswith('-oControlPersist=') for o in opts)
        assert any(o.startswith('-oServerAliveInterval=') for o in opts)
        assert any(o.startswith('-oServerAliveCountMax=') for o in opts)

    def test_windows_cygwin_ssh_keeps_multiplex(self, monkeypatch, tmp_path):
        # Cygwin/Git-Bash ssh on Windows reports the standard banner and has
        # working ControlMaster — so we DO add multiplex even though we're
        # on Windows. (Equivalent to "non-buggy ssh" in detection terms.)
        monkeypatch.setattr(sm, 'multiplex_known_broken', lambda: False)
        monkeypatch.setattr(sm, '_OUR_CONTROL_DIR', str(tmp_path / 'cm'))
        monkeypatch.setattr(sm, '_OUR_CONTROL_PATH', str(tmp_path / 'cm' / '%C'))
        probe = {'controlmaster': 'no', 'controlpath': 'none',
                 'serveraliveinterval': '0'}
        opts, we_own = sm.options_to_add(probe)
        assert we_own is True
        assert any(o.startswith('-oControlMaster=') for o in opts)

    def test_windows_user_configured_multiplex_respected(self, monkeypatch):
        # Even when the active ssh is the buggy one, if the user has multiplex
        # explicitly configured (e.g. via ~/.ssh/config pointing at Cygwin ssh)
        # we must respect their config, not override it.
        monkeypatch.setattr(sm, 'multiplex_known_broken', lambda: True)
        probe = {
            'controlmaster': 'auto', 'controlpath': '~/.ssh/sockets/%C',
            'serveraliveinterval': '30', 'serveralivecountmax': '5',
        }
        opts, we_own = sm.options_to_add(probe)
        assert we_own is False
        assert opts == [], 'user has full config — we add nothing'


class TestMultiplexKnownBroken:
    """`ssh -V` banner parsing for known-buggy clients."""

    @pytest.fixture(autouse=True)
    def _clear_cache(self):
        sm.multiplex_known_broken.cache_clear()
        yield
        sm.multiplex_known_broken.cache_clear()

    def test_non_windows_never_broken(self, monkeypatch):
        # On Linux/macOS we don't even probe — multiplex always works.
        monkeypatch.setattr(sm.System, 'windows', False)
        ep = mock.Mock()
        monkeypatch.setattr(sm, 'execute_piped', ep)
        assert sm.multiplex_known_broken() is False
        ep.assert_not_called()

    def test_microsoft_for_windows_banner_detected(self, monkeypatch):
        monkeypatch.setattr(sm.System, 'windows', True)
        monkeypatch.setattr(sm, 'execute_piped',
                            lambda *a, **k: 'OpenSSH_for_Windows_8.6p1, LibreSSL 3.4.3')
        assert sm.multiplex_known_broken() is True

    def test_cygwin_banner_not_broken(self, monkeypatch):
        monkeypatch.setattr(sm.System, 'windows', True)
        monkeypatch.setattr(sm, 'execute_piped',
                            lambda *a, **k: 'OpenSSH_9.6p1, OpenSSL 3.0.13 30 Jan 2024')
        assert sm.multiplex_known_broken() is False

    def test_result_is_cached(self, monkeypatch):
        monkeypatch.setattr(sm.System, 'windows', True)
        ep = mock.Mock(return_value='OpenSSH_for_Windows_8.6p1')
        monkeypatch.setattr(sm, 'execute_piped', ep)
        sm.multiplex_known_broken()
        sm.multiplex_known_broken()
        sm.multiplex_known_broken()
        assert ep.call_count == 1

    def test_ssh_missing_treated_as_broken_on_windows(self, monkeypatch):
        # Conservative default: if we can't even invoke ssh, don't risk
        # configuring multiplex on Windows.
        monkeypatch.setattr(sm.System, 'windows', True)
        # execute_piped(throw=False) returns None on failure; we treat that as
        # the conservative "skip mux" default.
        monkeypatch.setattr(sm, 'execute_piped', lambda *a, **k: None)
        assert sm.multiplex_known_broken() is True


class TestProbeSshConfig:
    def test_parses_keys(self):
        fake_out = (
            "user git\n"
            "hostname github.com\n"
            "ControlMaster auto\n"
            "ControlPath ~/.ssh/sockets/%C\n"
            "# comment line\n"
            "\n"
            "ServerAliveInterval 30\n"
        )
        fake_cp = mock.Mock(returncode=0, stdout=fake_out)
        with mock.patch('subprocess.run', return_value=fake_cp) as run:
            cfg = sm.probe_ssh_config(['git@github.com'])
            run.assert_called_once()
        assert cfg['user'] == 'git'
        assert cfg['hostname'] == 'github.com'
        assert cfg['controlmaster'] == 'auto'
        assert cfg['controlpath'] == '~/.ssh/sockets/%C'
        assert cfg['serveraliveinterval'] == '30'

    def test_returns_empty_on_failure(self):
        fake_cp = mock.Mock(returncode=255, stdout='', stderr='boom')
        with mock.patch('subprocess.run', return_value=fake_cp):
            assert sm.probe_ssh_config(['git@host']) == {}

    def test_returns_empty_on_timeout(self):
        import subprocess as sp
        with mock.patch('subprocess.run', side_effect=sp.TimeoutExpired('ssh', 5)):
            assert sm.probe_ssh_config(['git@host']) == {}


class TestEnsureMasterIdempotent:
    def test_runs_probe_once_per_host(self, monkeypatch):
        monkeypatch.setattr(sm, '_warmed', {})
        monkeypatch.setattr(sm, '_per_host_locks', {})

        probe_calls = []
        def fake_probe(args, timeout=5.0):
            probe_calls.append(list(args))
            return {'controlmaster': 'auto', 'controlpath': '/tmp/x'}
        monkeypatch.setattr(sm, 'probe_ssh_config', fake_probe)

        # User already has multiplex => we DON'T start a master, just remember.
        url = 'git@github.com:foo/bar.git'
        sm.ensure_master_for_url(url)
        sm.ensure_master_for_url(url)
        sm.ensure_master_for_url(url)
        assert len(probe_calls) == 1
        assert sm._warmed[('git', 'github.com', None)]['we_own_master'] is False

    def test_starts_master_when_user_lacks_config(self, monkeypatch, tmp_path):
        monkeypatch.setattr(sm, '_warmed', {})
        monkeypatch.setattr(sm, '_per_host_locks', {})
        monkeypatch.setattr(sm, '_OUR_CONTROL_DIR', str(tmp_path / 'cm'))
        monkeypatch.setattr(sm, '_OUR_CONTROL_PATH', str(tmp_path / 'cm' / '%C'))

        monkeypatch.setattr(sm, 'probe_ssh_config',
                            lambda args, timeout=5.0: {})

        master_calls = []
        def fake_start(user, host, port, opts):
            master_calls.append((user, host, port, list(opts)))
            return True
        monkeypatch.setattr(sm, '_start_master', fake_start)

        sm.ensure_master_for_url('git@example.com:foo.git')
        sm.ensure_master_for_url('git@example.com:bar.git')  # same host
        assert len(master_calls) == 1
        assert sm._warmed[('git', 'example.com', None)]['we_own_master'] is True

    def test_prewarm_failure_strips_multiplex_opts(self, monkeypatch, tmp_path):
        # When _start_master fails, we MUST clear ControlMaster/Path/Persist
        # from opts. Otherwise N parallel fetches would race to be the master
        # and trigger N concurrent auths — the exact thing this is meant to
        # prevent.
        monkeypatch.setattr(sm, '_warmed', {})
        monkeypatch.setattr(sm, '_per_host_locks', {})
        monkeypatch.setattr(sm, '_OUR_CONTROL_DIR', str(tmp_path / 'cm'))
        monkeypatch.setattr(sm, '_OUR_CONTROL_PATH', str(tmp_path / 'cm' / '%C'))
        monkeypatch.setattr(sm, 'probe_ssh_config',
                            lambda args, timeout=5.0: {})
        monkeypatch.setattr(sm, '_start_master',
                            lambda u, h, p, o: False)

        sm.ensure_master_for_url('git@example.com:foo.git')
        info = sm._warmed[('git', 'example.com', None)]
        assert info['we_own_master'] is False
        for o in info['opts']:
            assert not o.startswith('-oControlMaster=')
            assert not o.startswith('-oControlPath=')
            assert not o.startswith('-oControlPersist=')
        # Keepalives are still useful and stay.
        assert any(o.startswith('-oServerAliveInterval=') for o in info['opts'])

    def test_concurrent_ensure_probes_once(self, monkeypatch, tmp_path):
        """50 threads racing on the same host must result in exactly one probe
        and at most one master start."""
        import threading
        monkeypatch.setattr(sm, '_warmed', {})
        monkeypatch.setattr(sm, '_per_host_locks', {})
        monkeypatch.setattr(sm, '_OUR_CONTROL_DIR', str(tmp_path / 'cm'))
        monkeypatch.setattr(sm, '_OUR_CONTROL_PATH', str(tmp_path / 'cm' / '%C'))

        probe_count = [0]
        probe_lock = threading.Lock()
        def slow_probe(args, timeout=5.0):
            with probe_lock:
                probe_count[0] += 1
            # simulate the syscall being slow so threads pile up on the lock
            import time as _t; _t.sleep(0.05)
            return {'controlmaster': 'auto', 'controlpath': '/tmp/sock'}
        monkeypatch.setattr(sm, 'probe_ssh_config', slow_probe)

        start_event = threading.Event()
        def worker():
            start_event.wait()
            sm.ensure_master_for_url('git@example.com:proj.git')
        threads = [threading.Thread(target=worker) for _ in range(50)]
        for t in threads: t.start()
        start_event.set()
        for t in threads: t.join()
        assert probe_count[0] == 1


class TestWrapperPathSafety:
    """Regression: running mama_ssh.py as a script must not shadow stdlib
    modules. Earlier versions inserted `<...>/mama` onto sys.path, which made
    `mama/types/` shadow Python's stdlib `types` module — breaking `contextlib`
    on uv-installed Pythons that hadn't pre-imported it."""

    def test_invocation_does_not_put_mama_dir_on_syspath(self, tmp_path):
        import json
        import subprocess
        import textwrap
        wrapper = os.path.abspath(os.path.join(
            os.path.dirname(__file__), '..', '..', 'mama', 'utils', 'mama_ssh.py'))
        mama_dir = os.path.dirname(os.path.dirname(wrapper))
        # Subprocess so we get a fresh interpreter (no pre-cached `types` etc).
        # Monkey-patch os.execvp to a no-op BEFORE running the wrapper, so it
        # can't replace the process before we read sys.path back.
        probe = tmp_path / 'probe.py'
        probe.write_text(textwrap.dedent(f"""
            import json, os, sys
            os.execvp = lambda *a, **k: None
            sys.argv = [{wrapper!r}, 'git@example.com:foo.git', 'git-upload-pack']
            ns = {{'__name__': '__main__', '__package__': '', '__file__': {wrapper!r}}}
            with open({wrapper!r}) as f:
                code = f.read()
            try:
                exec(code, ns)
            except SystemExit:
                pass
            print('PATH_PROBE:' + json.dumps(sys.path))
        """))
        cp = subprocess.run([sys.executable, str(probe)],
                            capture_output=True, text=True, timeout=15)
        marker = [l for l in cp.stdout.splitlines() if l.startswith('PATH_PROBE:')]
        assert marker, f'probe did not produce output. stderr={cp.stderr!r}'
        path = json.loads(marker[-1][len('PATH_PROBE:'):])
        assert mama_dir not in path, (
            f'{mama_dir!r} ended up on sys.path — `mama/types/` would shadow '
            f'stdlib `types`. sys.path={path!r}')


class TestWrapperMain:
    """The wrapper passes options + destination unchanged to ssh -G, then
    exec's ssh with whatever extra -o flags are needed."""

    def test_passthrough_when_user_has_full_config(self, monkeypatch):
        from mama.utils import mama_ssh
        # Simulate ssh -G saying user has multiplex + keepalives configured.
        full = (
            "controlmaster auto\ncontrolpath /tmp/x\n"
            "serveraliveinterval 30\nserveralivecountmax 3\n"
        )
        monkeypatch.setattr(
            'subprocess.run',
            lambda *a, **k: mock.Mock(returncode=0, stdout=full),
        )
        execed: list = []
        monkeypatch.setattr('os.execvp',
                            lambda prog, argv: execed.extend([prog, argv]))
        mama_ssh.main(['mama_ssh.py', '-o', 'SendEnv=GIT_PROTOCOL',
                       'git@github.com', "git-upload-pack 'foo/bar.git'"])
        prog, argv = execed
        assert prog == 'ssh'
        # No options added — user already has everything.
        assert argv == ['ssh', '-o', 'SendEnv=GIT_PROTOCOL', 'git@github.com',
                        "git-upload-pack 'foo/bar.git'"]

    def test_adds_multiplex_when_user_has_nothing(self, monkeypatch, tmp_path):
        from mama.utils import mama_ssh
        monkeypatch.setattr(sm, '_OUR_CONTROL_DIR', str(tmp_path / 'cm'))
        monkeypatch.setattr(sm, '_OUR_CONTROL_PATH', str(tmp_path / 'cm' / '%C'))
        empty = "controlmaster no\ncontrolpath none\nserveraliveinterval 0\n"
        monkeypatch.setattr(
            'subprocess.run',
            lambda *a, **k: mock.Mock(returncode=0, stdout=empty),
        )
        execed: list = []
        monkeypatch.setattr('os.execvp',
                            lambda prog, argv: execed.extend([prog, argv]))
        mama_ssh.main(['mama_ssh.py', 'git@example.com', 'git-upload-pack'])
        prog, argv = execed
        assert prog == 'ssh'
        # Multiplex + keepalives are inserted before the original args.
        assert any(a.startswith('-oControlMaster=') for a in argv)
        assert any(a.startswith('-oControlPath=') for a in argv)
        assert any(a.startswith('-oServerAliveInterval=') for a in argv)
        assert argv[-2:] == ['git@example.com', 'git-upload-pack']


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv('GIT_SSH_COMMAND', raising=False)
