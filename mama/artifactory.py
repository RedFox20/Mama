import os, ftplib
import traceback
import keyring, getpass
from .util import console, download_file
from .system import System, execute_piped


def _get_commit_hash(target):
    result = None
    if os.path.exists(f'{target.dep.src_dir}/.git'):
        result = execute_piped(['git', 'show', '--format=%h', '-s'], cwd=target.dep.src_dir)
    return result if result else 'latest'


def artifactory_archive_name(target):
    """
    Constructs archive name for papa deploy packages in the form of:
    {name}-{platform}-{arch}-{build_type}-{commit_hash}
    Example: opencv-linux-x64-release-df76b66
    """
    name = target.name
    # triplets information to make this package platform unique
    platform = target.config.name() # eg 'windows', 'linux', 'oclea'
    arch = target.config.arch # eg 'x86', 'arm64'
    build_type = 'release' if target.config.release else 'debug'
    commit_hash = _get_commit_hash(target)
    return f'{name}-{platform}-{arch}-{build_type}-{commit_hash}'


if System.linux:
    from keyrings.cryptfile.cryptfile import CryptFileKeyring
    kr = CryptFileKeyring()
    kr.keyring_key = f'mamabuild-{os.getenv("USER")}'
    keyring.set_keyring(kr)


def _get_artifactory_ftp_credentials(config, url):
    if config.artifactory_auth == 'store':
        username = keyring.get_password('mamabuild', f'username-{url}')
        password = keyring.get_password('mamabuild', f'password-{url}')
        if username is not None:
            return username, password
    username = input(f'{url} username: ').strip()
    password = getpass.getpass(f'{username}@{url} password: ').strip()
    return username, password


def _remove_artifactory_ftp_credentials(url):
    if keyring.get_password('mamabuild', f'username-{url}'):
        keyring.delete_password('mamabuild', f'username-{url}')
    if keyring.get_password('mamabuild', f'password-{url}'):
        keyring.delete_password('mamabuild', f'password-{url}')


def _store_artifactory_ftp_credentials(config, url, username, password):
    if config.artifactory_auth == 'store':
        keyring.set_password('mamabuild', f'username-{url}', username)
        keyring.set_password('mamabuild', f'password-{url}', password)


def artifactory_ftp_login(ftp:ftplib.FTP_TLS, config, url):
    connected = False
    while True:
        username, password = _get_artifactory_ftp_credentials(config, url)
        if not connected:
            ftp.connect(url, timeout=5)
            connected = True
        try:
            ftp.login(username, password)
            _store_artifactory_ftp_credentials(config, url, username, password)
        except ftplib.Error as e:
            console(f'artifactory login failed: {e}')
            _remove_artifactory_ftp_credentials(url)
        else:
            return # success


def artifactory_sanitize_url(url):
    return url.replace('ftp://', '').replace('http://','').replace('https://','')


def artifactory_fetch(target):
    url = target.config.artifactory_ftp
    if not url: raise RuntimeError(f'Artifactory Fetch failed: artifactory_ftp not set by config.set_artifactory_ftp()')
    url = artifactory_sanitize_url(url)

    archive_name = artifactory_archive_name(target) + '.zip'
    remote_file = f'https://{url}/{archive_name}'
    download_file(remote_file, target.dep.build_dir, force=True)


def artifactory_upload(ftp:ftplib.FTP_TLS, file_path):
    size = os.path.getsize(file_path)
    transferred = 0
    lastpercent = 0
    with open(file_path, 'rb') as f:
        def print_progress(bytes):
            nonlocal transferred, lastpercent, size
            transferred += len(bytes)
            percent = int((transferred / size) * 100.0)
            if abs(lastpercent - percent) >= 5:
                lastpercent = percent
                n = int(percent / 2)
                left = '=' * n
                right = ' ' * int(50 - n)
                print(f'\r  |{left}>{right}| {percent:>3} %', end='')
        print(f'  |>{" ":50}| {0:>3} %', end='')
        ftp.storbinary(f'STOR {os.path.basename(file_path)}', f, callback=print_progress)
        print()


def artifactory_upload_ftp(target, file_path):
    config = target.config
    url = config.artifactory_ftp
    if not url: raise RuntimeError(f'Artifactory Upload failed: artifactory_ftp not set by config.set_artifactory_ftp()')
    if config.verbose: console(f'  - Artifactory Upload {file_path}\n {"":12}-> {url}')

    with ftplib.FTP_TLS() as ftp:
        try:
            # sanitize url for ftplib
            url = artifactory_sanitize_url(url)
            artifactory_ftp_login(ftp, config, url)
            #ftp.dir()
            #artifactory_upload(ftp, file_path)
        except:
            traceback.print_exc()
        finally:
            ftp.quit()

    artifactory_fetch(target)
