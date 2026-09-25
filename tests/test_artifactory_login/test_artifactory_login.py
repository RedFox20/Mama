"""Pins which credentials artifactory_ftp_login reads from, writes to and deletes from the keyring."""
import contextlib
import ftplib
import os
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from mama.artifactory import ArtifactoryCredentialsError, _get_keyring, artifactory_ftp_login
from mama.utils.system import System

URL = 'art.example.com'
TYPED = {f'username-{URL}': 'typed_user', f'password-{URL}': 'typed_pass'}


@pytest.fixture
def art(monkeypatch):
    """A dict keyring in `art.store`, a mock FTP server and a TTY prompt that types TYPED."""
    for name in ('MAMA_ARTIFACTORY_USER', 'MAMA_ARTIFACTORY_PASS'): monkeypatch.delenv(name, raising=False)
    store = {}
    keyring = Mock()
    keyring.get_password.side_effect = lambda _, key: store.get(key)
    keyring.set_password.side_effect = lambda _, key, value: store.__setitem__(key, value)
    keyring.delete_password.side_effect = lambda _, key: store.pop(key)
    monkeypatch.setattr('sys.stdin', Mock(isatty=lambda: True))
    monkeypatch.setattr('builtins.input', lambda _: 'typed_user')
    monkeypatch.setattr('getpass.getpass', lambda _: 'typed_pass')
    with patch('mama.artifactory._get_keyring', autospec=True, return_value=keyring) as get_keyring:
        yield SimpleNamespace(store=store, keyring=keyring, get_keyring=get_keyring, ftp=Mock(spec=ftplib.FTP_TLS))


@pytest.fixture
def ci(art, monkeypatch):
    monkeypatch.setenv('MAMA_ARTIFACTORY_USER', 'ci_user')
    monkeypatch.setenv('MAMA_ARTIFACTORY_PASS', 'ci_pass')
    return art


def login(art, rejects=0):
    art.ftp.login.side_effect = [ftplib.error_perm('530 Login incorrect')] * rejects + [None]
    artifactory_ftp_login(art.ftp, SimpleNamespace(artifactory_auth='store', verbose=False), URL)


def test_env_credentials_never_touch_the_keyring(ci):
    login(ci)
    ci.ftp.login.assert_called_once_with('ci_user', 'ci_pass')
    ci.get_keyring.assert_not_called()


def test_rejected_env_credentials_end_the_run(ci):
    with pytest.raises(ArtifactoryCredentialsError, match='MAMA_ARTIFACTORY_USER'): login(ci, rejects=1)
    ci.ftp.login.assert_called_once()
    ci.get_keyring.assert_not_called()


def test_stored_credentials_are_not_written_again(art):
    art.store.update({f'username-{URL}': 'u', f'password-{URL}': 'p'})
    login(art)
    art.ftp.login.assert_called_once_with('u', 'p')
    art.keyring.set_password.assert_not_called()


def test_typed_credentials_are_stored(art):
    login(art)
    assert art.store == TYPED


def test_rejected_stored_credentials_give_way_to_typed_ones(art):
    art.store.update({f'username-{URL}': 'stale', f'password-{URL}': 'stale'})
    login(art, rejects=1)
    assert art.store == TYPED


@pytest.fixture
def keyring_file(tmp_path, monkeypatch):
    """A keyring file in tmp_path that fails to parse. Yields its path and the cryptfile class."""
    if not System.linux: pytest.skip('only the Linux keyring is a cryptfile')
    monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path))
    monkeypatch.setattr('mama.artifactory.keyr', None)
    cryptfile = pytest.importorskip('keyrings.cryptfile.cryptfile').CryptFileKeyring
    monkeypatch.setattr(cryptfile, 'time_cost', 1)  # the default Argon2 cost takes 0.4 s per keyring call
    monkeypatch.setattr(cryptfile, 'memory_cost', 64)
    path = tmp_path / 'python_keyring' / 'cryptfile_pass.cfg'
    path.parent.mkdir()
    path.write_text('[mamabuild]\na = 1\n[mamabuild]\nb = 2\n')  # the DuplicateSectionError a racing write leaves
    yield path, cryptfile


def test_corrupt_keyring_file_moves_aside(keyring_file):
    path, _ = keyring_file
    _get_keyring().set_password('mamabuild', 'username-x', 'u')
    assert _get_keyring().get_password('mamabuild', 'username-x') == 'u'
    assert path.with_suffix('.cfg.corrupt').read_text().count('[mamabuild]') == 2


def test_keyring_healed_while_waiting_for_the_lock_stays(keyring_file):
    path, cryptfile = keyring_file

    @contextlib.contextmanager
    def lock_released_after_another_heal(lock_path, timeout):
        path.unlink()
        other = cryptfile()
        other.keyring_key = f'mamabuild-{os.getenv("USER")}'
        other.set_password('mamabuild', 'username-x', 'u')
        yield True

    with patch('mama.artifactory.interprocess_dir_lock', autospec=True, side_effect=lock_released_after_another_heal):
        assert _get_keyring().get_password('mamabuild', 'username-x') == 'u'
    assert not path.with_suffix('.cfg.corrupt').exists()
