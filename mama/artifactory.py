from __future__ import annotations
import os, ftplib, traceback, getpass
from typing import List, Tuple, TYPE_CHECKING

from .types.git import Git
from .types.local_source import LocalSource
from .types.artifactory_pkg import ArtifactoryPkg
from .types.dep_source import DepSource
from .types.asset import Asset
from .utils.system import System, console
from .utils.sub_process import execute_piped
import mama.package as package
from .util import download_file, normalized_join, read_lines_from, try_unzip


if TYPE_CHECKING:
    from .build_target import BuildTarget
    from .build_config import BuildConfig


def _get_commit_hash(target:BuildTarget):
    result = None
    src_dir = target.source_dir()
    if os.path.exists(f'{src_dir}/.git'):
        result = execute_piped(['git', 'show', '--format=%h', '-s'], cwd=src_dir)
    return result if result else 'latest'


def artifactory_archive_name(target:BuildTarget):
    """
    Constructs archive name for papa deploy packages in the form of:
    {name}-{platform}-{compiler}-{arch}-{build_type}-{commit_hash}
    Example: opencv-linux-x64-gcc9-release-df76b66
    """
    p:ArtifactoryPkg = target.dep.dep_source
    if p.is_pkg and p.fullname:
        return p.fullname

    name = target.name
    # triplets information to make this package platform unique
    platform = target.config.name() # eg 'windows', 'linux', 'oclea'
    compiler = target.config.compiler_version()
    arch = target.config.arch # eg 'x86', 'arm64'
    build_type = 'release' if target.config.release else 'debug'
    commit_hash = p.version if p.is_pkg else _get_commit_hash(target)
    return f'{name}-{platform}-{compiler}-{arch}-{build_type}-{commit_hash}'


keyr = None
def _get_keyring():
    global keyr
    if not keyr: # lazy init keyring, because it loads certs and other slow stuff
        import keyring
        if System.linux:
            import importlib
            cryptfile = importlib.import_module('keyrings.cryptfile.cryptfile')
            kr = cryptfile.CryptFileKeyring()
            kr.keyring_key = f'mamabuild-{os.getenv("USER")}'
            keyring.set_keyring(kr)
        keyr = keyring
    return keyr


def _get_artifactory_ftp_credentials(config:BuildConfig, url:str):
    # get the values from ENV
    username = os.getenv('MAMA_ARTIFACTORY_USER', None)
    password = os.getenv('MAMA_ARTIFACTORY_PASS', '')  # empty password as default
    if username is not None:
        return username, password

    if config.artifactory_auth == 'store':
        username = _get_keyring().get_password('mamabuild', f'username-{url}')
        password = _get_keyring().get_password('mamabuild', f'password-{url}')
        if username is not None:
            return username, password

    username = input(f'{url} username: ').strip()
    if not username:
        raise EnvironmentError(f'Artifactory user missing: try setting MAMA_ARTIFACTORY_USER env variable.')

    password = getpass.getpass(f'{username}@{url} password: ').strip()
    if password is None: # None on CI
        raise EnvironmentError(f'Artifactory user missing: try setting MAMA_ARTIFACTORY_PASS env variable.')
    return username, password


def _remove_artifactory_ftp_credentials(url:str):
    if _get_keyring().get_password('mamabuild', f'username-{url}'):
        _get_keyring().delete_password('mamabuild', f'username-{url}')
    if _get_keyring().get_password('mamabuild', f'password-{url}'):
        _get_keyring().delete_password('mamabuild', f'password-{url}')


def _store_artifactory_ftp_credentials(config:BuildConfig, url, username, password):
    if config.artifactory_auth == 'store':
        _get_keyring().set_password('mamabuild', f'username-{url}', username)
        _get_keyring().set_password('mamabuild', f'password-{url}', password)


def artifactory_ftp_login(ftp:ftplib.FTP_TLS, config:BuildConfig, url:str):
    connected = False
    while True:
        username, password = _get_artifactory_ftp_credentials(config, url)
        if not connected:
            if config.verbose:
                console(f'  - Artifactory Connect {url}')
            ftp.connect(url, timeout=60)
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


def artifactory_upload(ftp:ftplib.FTP_TLS, file_path:str):
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
                print(f'\r    |{left}>{right}| {percent:>3} %', end='')
        print(f'    |>{" ":50}| {0:>3} %', end='')
        ftp.storbinary(f'STOR {os.path.basename(file_path)}', f, callback=print_progress)
        print(f'\r    |{"="*50}>| 100 %')


def artifact_already_exists(ftp:ftplib.FTP_TLS, file_path:str):
    items = []
    ftp.dir(os.path.basename(file_path), items.append)
    return len(items) > 0 # the file already exists


def artifactory_upload_ftp(target:BuildTarget, file_path:str) -> bool:
    config = target.config
    url = config.artifactory_ftp
    if not url: raise RuntimeError(f'Artifactory Upload failed: artifactory_ftp not set by config.set_artifactory_ftp()')
    if config.verbose: console(f'  - Artifactory Upload {file_path}\n {"":12}-> {url}')

    with ftplib.FTP_TLS() as ftp:
        try:
            # sanitize url for ftplib
            url = artifactory_sanitize_url(url)
            artifactory_ftp_login(ftp, config, url)
            if config.if_needed and artifact_already_exists(ftp, file_path):
                if config.verbose: console(f'  - Artifactory Upload skipped: artifact already exists')
                return False # skip upload
            artifactory_upload(ftp, file_path)
            return True
        except:
            traceback.print_exc()
        finally:
            ftp.quit()
    return False


def make_dep_source(s:str) -> DepSource:
    if s.startswith('git '): return Git.from_papa_string(s[4:])
    if s.startswith('pkg '): return ArtifactoryPkg.from_papa_string(s[4:])
    if s.startswith('src '): return LocalSource.from_papa_string(s[4:])
    raise RuntimeError(f'Unrecognized dependency source: {s}')


def artifactory_reconfigure_target_from_deployment(target:BuildTarget, deploy_path) -> Tuple[bool, list]:
    """
    Reconfigures `target` from {deployment_path}/papa.txt.
    Returns (fetched:bool, dep_sources:list)
    """
    papa_list = normalized_join(deploy_path, 'papa.txt')
    if not os.path.exists(papa_list):
        console(f'Artifactory Reconfigure Target={target.name} failed because {papa_list} does not exist')
        return (False, None)

    if target.config.verbose:
        console(f'    Artifactory reconfigure target={target.name}')

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

    target.dep.from_artifactory = True
    target.exported_includes = includes # include folders to export from this target
    target.exported_assets = assets # exported asset files
    package.set_export_libs_and_products(target, libs)
    package.reload_syslibs(target, syslibs) # set exported system libraries
    return (True, dependencies)


def _fetch_package(target:BuildTarget, url, archive, build_dir):
    remote_file = f'https://{url}/{archive}.zip'
    try:
        return download_file(remote_file, build_dir, force=True, 
                             message=f'    Artifactory fetch {url}/{archive} ')
    except Exception as e:
        if target.config.verbose:
            console(f'    Artifactory fetch failed with {e} {url}/{archive}.zip')
        # this is an artifactory pkg, so the url MUST exist
        if target.dep.dep_source.is_pkg:
            raise RuntimeError(f'Artifactory package {target.dep.dep_source} did not exist at {url}')
        return None


def unzip_and_reconfigure(target:BuildTarget, local_file:str, build_dir:str) -> Tuple[bool, list]:
    if try_unzip(local_file, build_dir):
        return artifactory_reconfigure_target_from_deployment(target, build_dir)
    else:
        console(f'    Artifactory unzip failed, possibly corrupt package {local_file}')
        os.remove(local_file) # it's probably corrupted
        return (False, None)


def artifactory_fetch_and_reconfigure(target:BuildTarget) -> Tuple[bool, list]:
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
        if (target.is_current_target() or target.config.no_specific_target()) \
            and not target.test:
            console(f'    Artifactory cache {local_file}')
        success, deps = unzip_and_reconfigure(target, local_file, build_dir)
        if success: return (success, deps)

    # use mama verbose to show the failure msgs
    local_file = _fetch_package(target, url, archive, build_dir)
    if not local_file:
        return (False, None)
    console(f'    Artifactory unzip {archive}')
    return unzip_and_reconfigure(target, local_file, build_dir)

