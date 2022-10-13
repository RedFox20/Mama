import os, ftplib, traceback, getpass, keyring
from typing import List, Tuple

from .types.git import Git
from .types.local_source import LocalSource
from .types.dep_source import DepSource
from .types.asset import Asset
from .types.artifactory_pkg import ArtifactoryPkg
from .system import System, execute_piped
from .util import console, download_file, normalized_join, read_lines_from, unzip

def _get_commit_hash(target):
    result = None
    if os.path.exists(f'{target.source_dir()}/.git'):
        result = execute_piped(['git', 'show', '--format=%h', '-s'], cwd=target.source_dir())
    return result if result else 'latest'


def artifactory_archive_name(target):
    """
    Constructs archive name for papa deploy packages in the form of:
    {name}-{platform}-{arch}-{build_type}-{commit_hash}
    Example: opencv-linux-x64-release-df76b66
    """
    pkg:ArtifactoryPkg = target.dep.dep_source
    if pkg.is_pkg and pkg.fullname:
        return pkg.fullname

    name = target.name
    # triplets information to make this package platform unique
    platform = target.config.name() # eg 'windows', 'linux', 'oclea'
    arch = target.config.arch # eg 'x86', 'arm64'
    build_type = 'release' if target.config.release else 'debug'
    commit_hash = pkg.version if pkg.is_pkg else _get_commit_hash(target)
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
            artifactory_upload(ftp, file_path)
        except:
            traceback.print_exc()
        finally:
            ftp.quit()


def make_dep_source(s:str) -> DepSource:
    if s.startswith('git '): return Git.from_papa_string(s[4:])
    if s.startswith('pkg '): return ArtifactoryPkg.from_papa_string(s[4:])
    if s.startswith('src '): return LocalSource.from_papa_string(s[4:])
    raise RuntimeError(f'Unrecognized dependency source: {s}')


def artifactory_reconfigure_target_from_deployment(target, deploy_path) -> Tuple[bool, list]:
    """
    Reconfigures `target` from {deployment_path}/papa.txt.
    Returns (fetched:bool, dep_sources:list)
    """
    papa_list = normalized_join(deploy_path, 'papa.txt')
    if not os.path.exists(papa_list):
        console(f'Artifactory Reconfigure Target={target.name} failed because {papa_list} does not exist')
        return (False, None)

    project_name = None
    dependencies: list = []
    includes: List[str] = []
    libs: List[str] = []
    syslibs: List[str] = []
    assets: List[Asset] = []

    def append_to(to:list, line):
        to.append(normalized_join(deploy_path, line[2:].strip()))

    for line in read_lines_from(papa_list):
        if   line.startswith('P '): project_name = line[2:].strip()
        elif line.startswith('D '): dependencies.append(make_dep_source(line[2:].strip()))
        elif line.startswith('I '): append_to(includes, line)
        elif line.startswith('L '): append_to(libs, line)
        elif line.startswith('S '): append_to(syslibs, line)
        elif line.startswith('A '):
            relpath = line[2:].strip()
            fullpath = normalized_join(deploy_path, relpath)
            assets.append(Asset(relpath, fullpath, None))

    if project_name != target.name:
        console(f'Artifactory Reconfigure Target={target.name} failed because {papa_list} ProjectName={project_name} mismatches!')
        return (False, None)

    print(f'dependencies = {dependencies}')
    target.exported_includes = includes # include folders to export from this target
    target.exported_libs     = libs # libs to export from this target
    target.exported_syslibs  = syslibs # exported system libraries
    target.exported_assets   = assets # exported asset files
    return (True, dependencies)


def _fetch_package(target, url, archive, build_dir):
    remote_file = f'https://{url}/{archive}.zip'
    try:
        return download_file(remote_file, build_dir, force=True, 
                             message=f'Artifactory fetch {url}/{archive} ')
    except Exception as e:
        if target.config.verbose:
            console(f'Artifactory fetch failed with {e} {url}/{archive}.zip')
        # this is an artifactory pkg, so the url MUST exist
        if target.dep.dep_source.is_pkg:
            raise RuntimeError(f'Artifactory package {target.dep.dep_source} did not exist at {url}')
        return None


def artifactory_fetch_and_reconfigure(target) -> Tuple[bool, list]:
    """
    Try to fetch prebuilt package from artifactory
    Returns (fetched:bool, dep_sources:list)
    """
    url = target.config.artifactory_ftp
    if not url:
        return (False, None)

    url = artifactory_sanitize_url(url)
    archive = artifactory_archive_name(target)
    build_dir = target.build_dir()
    local_file = normalized_join(build_dir, f'{archive}.zip')

    if os.path.exists(local_file):
        console(f'Artifactory using existing Package at: {local_file}')
    else:
        local_file = _fetch_package(target, url, archive, build_dir)
        if not local_file:
            return (False, None)

    unzip(local_file, build_dir)
    console(f'Artifactory unzip {archive}')
    return artifactory_reconfigure_target_from_deployment(target, build_dir)
