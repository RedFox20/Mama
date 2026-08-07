"""ssh_multiplex pure-logic: URL parsing, options decision, wrapper arg parsing."""
import os
import subprocess
import sys
from unittest import mock
from unittest.mock import Mock

import pytest

from mama.utils import ssh_multiplex as sm


@pytest.mark.parametrize('url, endpoint', [
    ('git@github.com:foo/bar.git',        ('git', 'github.com', None)),
    ('alice@host.example:proj.git',       ('alice', 'host.example', None)),
    ('host.example:proj.git',             ('git', 'host.example', None)),   # scp form with no user
    ('ssh://git@host:2222/foo/bar.git',   ('git', 'host', '2222')),
    ('ssh://host/foo/bar.git',            ('git', 'host', None)),
    ('https://github.com/foo/bar.git',    None),
    ('http://github.com/foo/bar.git',     None),
    ('file:///srv/repos/foo.git',         None),
    ('/srv/repos/foo.git',                None),
    ('foo/bar.git',                       None),   # no colon, so neither scp form nor a scheme
    ('',                                  None),
    (None,                                None),
    ('C:/foo/bar',                        None),   # a drive letter also reads as an scp-form host
    ('D:\\repos\\proj',                   None),
    ('git@host:',                         None),   # a host with no path names no repository
    ('git@[::1]:repo.git',                None),   # git itself refuses scp-form bracketed IPv6
])
def test_only_a_real_ssh_url_names_an_endpoint(url, endpoint):
    assert sm.parse_ssh_endpoint(url) == endpoint


@pytest.mark.parametrize('probe, configured', [
    ({'controlmaster': 'auto', 'controlpath': '~/.ssh/cm/%C'}, True),
    ({'controlmaster': 'yes', 'controlpath': '/tmp/sock'},     True),
    ({'controlmaster': 'no', 'controlpath': 'none'},           False),
    ({'controlmaster': 'auto', 'controlpath': 'none'},         False),  # no socket, whatever the master says
    ({'controlmaster': 'no', 'controlpath': '/tmp/sock'},      False),
    ({},                                                       False),  # a failed `ssh -G` is not a config
])
def test_multiplex_needs_both_a_master_and_a_socket(probe, configured):
    assert sm.is_multiplex_configured(probe) is configured


class TestOptionsToAdd:
    def test_user_has_full_config(self):
        probe = {
            'controlmaster': 'auto',
            'controlpath': '~/.ssh/sockets/%C',
            'serveraliveinterval': '30',
            'serveralivecountmax': '5',
        }
        opts, we_own = sm.options_to_add(probe)
        assert opts == sm._SAFETY_OPTS, 'only the always-on safety opts when user has everything'
        assert we_own is False

    def test_user_has_nothing(self, tmp_path, monkeypatch):
        # pin the multiplex-enabled path. The native-Windows skip has its own tests.
        monkeypatch.setattr(sm.System, 'windows', False)
        # avoid mkdir on the user's actual ~/.ssh/cm
        probe = {'controlmaster': 'no', 'controlpath': 'none'}
        opts, we_own = sm.options_to_add(probe)
        assert we_own is True
        assert any(o.startswith('-oControlMaster=') for o in opts)
        assert any(o.startswith('-oControlPath=') for o in opts)
        assert any(o.startswith('-oControlPersist=') for o in opts)
        assert any(o.startswith('-oServerAliveInterval=') for o in opts)
        assert any(o.startswith('-oServerAliveCountMax=') for o in opts)

    def test_user_has_keepalives_only(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sm.System, 'windows', False)  # pin the multiplex-enabled path
        probe = {
            'controlmaster': 'no', 'controlpath': 'none',
            'serveraliveinterval': '60', 'serveralivecountmax': '3',
        }
        opts, we_own = sm.options_to_add(probe)
        assert we_own is True
        # multiplex added, keepalives not: the user already has them
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
        assert not any(o.startswith('-oControlMaster=') for o in opts)
        assert not any(o.startswith('-oControlPath=') for o in opts)
        assert any(o.startswith('-oServerAliveInterval=') for o in opts)

    def test_windows_skips_multiplex_keeps_keepalives(self, monkeypatch, tmp_path):
        # Microsoft OpenSSH ControlMaster is unreliable, so native Windows skips multiplex entirely. Keepalives still help.
        monkeypatch.setattr(sm.System, 'windows', True)
        probe = {'controlmaster': 'no', 'controlpath': 'none',
                 'serveraliveinterval': '0'}
        opts, we_own = sm.options_to_add(probe)
        assert we_own is False
        assert not any(o.startswith('-oControlMaster=') for o in opts)
        assert not any(o.startswith('-oControlPath=') for o in opts)
        assert not any(o.startswith('-oControlPersist=') for o in opts)
        assert any(o.startswith('-oServerAliveInterval=') for o in opts)
        assert any(o.startswith('-oServerAliveCountMax=') for o in opts)

    def test_windows_user_configured_multiplex_respected(self, monkeypatch):
        # a user who configured multiplex (for example via Cygwin ssh) keeps their config untouched, even on Windows
        monkeypatch.setattr(sm.System, 'windows', True)
        probe = {
            'controlmaster': 'auto', 'controlpath': '~/.ssh/sockets/%C',
            'serveraliveinterval': '30', 'serveralivecountmax': '5',
        }
        opts, we_own = sm.options_to_add(probe)
        assert we_own is False
        assert opts == sm._SAFETY_OPTS, 'user has full config - only the always-on safety opts'


class TestMultiplexKnownBroken:
    """WSL, Cygwin and Git-Bash run as Linux from Python (System.windows is False), so only
    native Windows disables multiplex."""

    def test_non_windows_not_broken(self, monkeypatch):
        monkeypatch.setattr(sm.System, 'windows', False)
        assert sm.multiplex_known_broken() is False

    def test_windows_is_broken(self, monkeypatch):
        monkeypatch.setattr(sm.System, 'windows', True)
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

        # the user already has multiplex, so no master is started, only remembered
        url = 'git@github.com:foo/bar.git'
        sm.ensure_master_for_url(url)
        sm.ensure_master_for_url(url)
        sm.ensure_master_for_url(url)
        assert len(probe_calls) == 1
        assert sm._warmed[('git', 'github.com', None)]['we_own_master'] is False

    def test_starts_master_when_user_lacks_config(self, monkeypatch, tmp_path):
        monkeypatch.setattr(sm.System, 'windows', False)  # pin the multiplex-enabled path
        monkeypatch.setattr(sm, '_warmed', {})
        monkeypatch.setattr(sm, '_per_host_locks', {})

        monkeypatch.setattr(sm, 'probe_ssh_config',
                            lambda args, timeout=5.0: {})

        master_calls = []
        def fake_open(user, host, port, opts):
            master_calls.append((user, host, port, list(opts)))
            return sm._MASTER_STARTED
        monkeypatch.setattr(sm, '_open_master', fake_open)

        sm.ensure_master_for_url('git@example.com:foo.git')
        sm.ensure_master_for_url('git@example.com:bar.git')  # same host
        assert len(master_calls) == 1
        assert sm._warmed[('git', 'example.com', None)]['we_own_master'] is True

    def test_prewarm_failure_strips_multiplex_opts(self, monkeypatch, tmp_path):
        # When _open_master fails, Control* must leave opts, or N parallel fetches race to be the master and auth N times.
        monkeypatch.setattr(sm, '_warmed', {})
        monkeypatch.setattr(sm, '_per_host_locks', {})
        monkeypatch.setattr(sm, 'probe_ssh_config',
                            lambda args, timeout=5.0: {})
        monkeypatch.setattr(sm, '_open_master',
                            lambda u, h, p, o: sm._MASTER_FAILED)

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
        import threading
        monkeypatch.setattr(sm, '_warmed', {})
        monkeypatch.setattr(sm, '_per_host_locks', {})

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
    """Running mama_ssh.py as a script must not put the mama dir on sys.path.
    There `mama/types/` shadows the stdlib `types` module and breaks `contextlib`."""

    def test_invocation_does_not_put_mama_dir_on_syspath(self, tmp_path):
        import json
        import subprocess
        import textwrap
        wrapper = os.path.abspath(os.path.join(
            os.path.dirname(__file__), '..', '..', 'mama', 'utils', 'mama_ssh.py'))
        mama_dir = os.path.dirname(os.path.dirname(wrapper))
        # A fresh interpreter has no pre-cached `types`. A no-op os.execvp lets the probe read sys.path back after the wrapper.
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
            f'{mama_dir!r} ended up on sys.path - `mama/types/` would shadow '
            f'stdlib `types`. sys.path={path!r}')


class TestWrapperMain:
    def test_passthrough_when_user_has_full_config(self, monkeypatch):
        from mama.utils import mama_ssh
        # ssh -G reports that the user has multiplex and keepalives configured
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
        # only the always-on safety opts are added: the user has the rest
        assert argv == ['ssh', *sm._SAFETY_OPTS, '-o', 'SendEnv=GIT_PROTOCOL', 'git@github.com',
                        "git-upload-pack 'foo/bar.git'"]

    def test_adds_multiplex_when_user_has_nothing(self, monkeypatch, tmp_path):
        from mama.utils import mama_ssh
        monkeypatch.setattr(sm.System, 'windows', False)  # pin the multiplex-enabled path
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
        assert any(a.startswith('-oControlMaster=') for a in argv)
        assert any(a.startswith('-oControlPath=') for a in argv)
        assert any(a.startswith('-oServerAliveInterval=') for a in argv)
        assert argv[-2:] == ['git@example.com', 'git-upload-pack']


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv('GIT_SSH_COMMAND', raising=False)


@pytest.fixture(autouse=True)
def _our_control_dir(monkeypatch, tmp_path):
    """Keep every socket this file writes under tmp_path. The module resolves the real dir at import,
    and a test that opens a master there would litter the home dir of the developer."""
    control_dir = str(tmp_path / 'cm')
    monkeypatch.setattr(sm, '_OUR_CONTROL_DIR', control_dir)
    monkeypatch.setattr(sm, '_OUR_CONTROL_PATH', f'{control_dir}/%C')


class TestMasterOwnership:
    """`ssh -O check` decides ownership. Getting it wrong makes mama `ssh -O exit` a master another
    parallel job owns, killing its fetches mid-transfer."""

    def _opts(self, tmp_path):
        return ['-oControlMaster=auto', f'-oControlPath={tmp_path}/cm/%C', '-oControlPersist=10m']

    def test_a_live_master_is_adopted_not_restarted(self, monkeypatch, tmp_path):
        monkeypatch.setattr(sm, '_master_alive', lambda u, h, p, o: True)
        monkeypatch.setattr(subprocess, 'run', lambda *a, **k: pytest.fail('must not start a second master'))
        assert sm._open_master('git', 'example.com', None, self._opts(tmp_path)) == sm._MASTER_ADOPTED

    def test_an_adopted_master_is_never_ours_to_close(self, monkeypatch, tmp_path):
        monkeypatch.setattr(sm.System, 'windows', False)
        monkeypatch.setattr(sm, '_warmed', {})
        monkeypatch.setattr(sm, '_per_host_locks', {})
        monkeypatch.setattr(sm, 'probe_ssh_config', lambda args, timeout=5.0: {})
        monkeypatch.setattr(sm, '_open_master', lambda u, h, p, o: sm._MASTER_ADOPTED)

        sm.ensure_master_for_url('git@example.com:foo.git')
        info = sm._warmed[('git', 'example.com', None)]
        assert info['we_own_master'] is False
        assert any(o.startswith('-oControlPath=') for o in info['opts'])  # ...but we still use the socket

    def test_a_dead_socket_is_removed_before_starting(self, monkeypatch, tmp_path):
        # ssh refuses to open a master while the file exists, even with nothing listening on it
        sock = tmp_path / 'cm' / 'deadbeef'
        sock.parent.mkdir(parents=True)
        sock.touch()
        monkeypatch.setattr(sm, '_master_alive', lambda u, h, p, o: False)
        monkeypatch.setattr(sm, 'probe_ssh_config', lambda args, timeout=5.0: {'controlpath': str(sock)})
        monkeypatch.setattr(sm, '_wait_master_ready', lambda u, h, p, o: True)
        monkeypatch.setattr(subprocess, 'run', lambda *a, **k: Mock(returncode=0))
        assert sm._open_master('git', 'example.com', None, self._opts(tmp_path)) == sm._MASTER_STARTED
        assert not sock.exists()

    def test_a_socket_outside_our_control_dir_is_left_alone(self, monkeypatch, tmp_path):
        theirs = tmp_path / 'user' / 'sock'
        theirs.parent.mkdir(parents=True)
        theirs.touch()
        monkeypatch.setattr(sm, 'probe_ssh_config', lambda args, timeout=5.0: {'controlpath': str(theirs)})
        sm._remove_stale_socket('git', 'example.com', None, self._opts(tmp_path))
        assert theirs.exists()  # a user-configured ControlPath is theirs to manage


def test_the_fetch_semaphore_is_clamped_to_the_session_limit(monkeypatch):
    """`parallel_max` also sizes the scheduler's LOAD pool, where artifactory downloads want a high
    number. Every git session past the server's MaxSessions is refused outright on the shared master,
    so the git cap cannot follow it up."""
    monkeypatch.setattr(sm, '_fetch_semaphore', None)
    sm.init_fetch_semaphore(40)
    assert sm._fetch_semaphore._value == sm.DEFAULT_MAX_CONCURRENT_FETCHES


def test_an_unwritable_control_dir_disables_multiplex_instead_of_raising(monkeypatch, tmp_path):
    """A CI container often runs as a uid that does not own $HOME (GitHub Actions: `/github/home/.ssh`
    gives Errno 13). Multiplexing is an optimization and must never be what fails a build."""
    monkeypatch.setattr(sm.System, 'windows', False)
    def denied(*a, **k): raise PermissionError(13, 'Permission denied')
    monkeypatch.setattr(sm.os, 'makedirs', denied)

    opts, we_own_master = sm.options_to_add({})
    assert we_own_master is False
    assert not any(o.startswith('-oControlMaster=') or o.startswith('-oControlPath=') for o in opts)
    assert any(o.startswith('-oServerAliveInterval=') for o in opts)  # keepalives still help


class TestControlDir:
    """Where the control sockets live: a temp dir, so a container can mount ~/.ssh read-only."""

    def test_a_normal_session_keeps_the_sockets_out_of_ssh(self, monkeypatch):
        monkeypatch.delenv('XDG_RUNTIME_DIR', raising=False)
        monkeypatch.setattr(sm.tempfile, 'gettempdir', lambda: '/tmp')
        assert sm._control_dir_candidates()[0].startswith(f'/tmp/{sm._CONTROL_SUBDIR}-')

    def test_the_runtime_dir_wins_when_the_session_has_one(self, monkeypatch):
        monkeypatch.setenv('XDG_RUNTIME_DIR', '/run/user/1000')
        assert sm._control_dir_candidates()[0] == f'/run/user/1000/{sm._CONTROL_SUBDIR}'

    def test_a_long_temp_dir_falls_back_to_a_short_one(self, monkeypatch):
        # macOS $TMPDIR is /var/folders/<2>/<27>/T/, long enough to exceed the socket budget with %C
        monkeypatch.delenv('XDG_RUNTIME_DIR', raising=False)
        monkeypatch.setattr(sm.tempfile, 'gettempdir', lambda: '/var/folders/ab/' + 'x' * 80 + '/T')
        assert sm._control_dir_candidates()[0].startswith('/tmp/')

    def test_every_candidate_fits_the_socket_limit(self, monkeypatch):
        # ssh expands %C to 40 hex chars, and a UNIX socket path caps at 104 bytes on macOS
        monkeypatch.delenv('XDG_RUNTIME_DIR', raising=False)
        assert all(len(d) + 41 <= sm._MAX_SOCKET_PATH for d in sm._control_dir_candidates())

    def test_an_unusable_first_choice_moves_the_socket_to_the_next_one(self, monkeypatch, tmp_path):
        blocked = tmp_path / 'file'; blocked.write_text('')  # makedirs under a file raises
        good = str(tmp_path / 'good')
        monkeypatch.setattr(sm, '_OUR_CONTROL_DIR', str(blocked / 'cm'))
        monkeypatch.setattr(sm, '_OUR_CONTROL_PATH', str(blocked / 'cm' / '%C'))
        monkeypatch.setattr(sm, '_control_dir_candidates', lambda: [good])
        assert sm._control_dir_usable()
        assert sm._OUR_CONTROL_DIR == good
        assert sm._OUR_CONTROL_PATH == f'{good}/%C'

    @pytest.mark.linux_host
    def test_the_dir_is_private(self, monkeypatch, tmp_path):
        assert sm._control_dir_usable()
        assert oct(os.stat(tmp_path / 'cm').st_mode)[-3:] == '700'
