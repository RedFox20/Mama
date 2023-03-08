from __future__ import annotations
import os, ftplib, traceback, getpass
from typing import List, Tuple, TYPE_CHECKING
from urllib.error import HTTPError

from .types.git import Git
from .types.local_source import LocalSource
from .types.artifactory_pkg import ArtifactoryPkg
from .types.dep_source import DepSource
from .types.asset import Asset
from .utils.system import Color, System, console, error
import mama.package as package
from .util import download_file, normalized_join, read_lines_from, try_unzip


if TYPE_CHECKING:
    from .build_target import BuildTarget
    from .build_config import BuildConfig


def artifactory_archive_name(target:BuildTarget):
    """
    Constructs archive name for papa deploy packages in the form of:
    {name}-{platform}-{compiler}-{arch}-{build_type}-{commit_hash}
    Example: opencv-linux-x64-gcc9-release-df76b66
    """
    p:ArtifactoryPkg = target.dep.dep_source

    # LocalSource has no archive name, except for ROOT packages
    if p.is_src and not target.dep.is_root:
        return None

    # if this is an ArtifactoryPkg with full name of the archive
    if p.is_pkg and p.fullname:
        return p.fullname

    # automatically build name of the package
    version = ''
    if target.dep.is_root:
        version = Git.get_current_repository_commit(target.dep)
        if not version:
            return None # nothing to do at this point
    elif p.is_pkg:
        version = p.version
    elif p.is_git:
        git:Git = p
        version = git.get_commit_hash(target.dep)
        if not version:
            return None # nothing to do at this point

    name = target.name
    # triplets information to make this package platform unique
    platform, os_major, os_minor = target.config.get_distro_info()
    compiler = target.config.compiler_version()
    arch = target.config.arch # eg 'x86', 'arm64'
    build_type = 'release' if target.config.release else 'debug'

    return f'{name}-{platform}-{os_major}-{compiler}-{arch}-{build_type}-{version}'


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


def artifactory_sanitize_url(url: str):
    return url.replace('ftp://', '').replace('http://','').replace('https://','')


def artifactory_upload(ftp:ftplib.FTP_TLS, target_name:str, file_path:str):
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
        # chdir into FTP_ROOT/target_name/
        try:
            ftp.cwd(target_name)
        except:
            ftp.mkd(target_name) # create subdirectory if needed
            ftp.cwd(target_name)
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
            artifactory_upload(ftp, target.name, file_path)
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


def artifactory_load_target(target:BuildTarget, deploy_path, num_files_copied) -> Tuple[bool, list]:
    """
    Reconfigures `target` from {deployment_path}/papa.txt.
    Returns (fetched:bool, dep_sources:list)
    """
    papa_list = normalized_join(deploy_path, 'papa.txt')
    if not os.path.exists(papa_list):
        error(f'    {target.name}  Artifactory Load failed because {papa_list} does not exist')
        return (False, None)

    if target.config.verbose:
        if num_files_copied != 0:
            console(f'    {target.name}  Artifactory Load ({num_files_copied} files were copied)', color=Color.YELLOW)
        else:
            console(f'    {target.name}  Artifactory Load (no files modified)', color=Color.GREEN)

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
        error(f'    {target.name}  Artifactory Load failed because {papa_list} ProjectName={project_name} mismatches!')
        return (False, None)

    target.dep.from_artifactory = True
    target.exported_includes = includes # include folders to export from this target
    target.exported_assets = assets # exported asset files
    package.set_export_libs_and_products(target, libs)
    package.reload_syslibs(target, syslibs) # set exported system libraries

    # for git repos, save the used commit hash status, so that next time the fetch is faster
    if target.dep.dep_source.is_git:
        git: Git = target.dep.dep_source
        git.save_status(target.dep)

    return (True, dependencies)


def _fetch_package(target:BuildTarget, url, archive, cache_dir):
    remote_file = f'http://{url}/{target.name}/{archive}.zip'
    try:
        return download_file(remote_file, cache_dir, force=True, 
                             message=f'    Artifactory fetch {url}/{archive} ')
    except Exception as e:
        if target.config.verbose or target.config.force_artifactory:
            error(f'    Artifactory fetch failed with {e} {url}/{archive}.zip')

        d:DepSource = target.dep.dep_source
        # this is an artifactory pkg, so the url MUST exist
        if d.is_pkg:
            raise RuntimeError(f'Artifactory package {d} did not exist at {url}')

        # if server gives us 404, then we need to wipe the git_status and re-initialize
        # the dependency source from scratch
        if d.is_git:
            d: Git = d
            if isinstance(e, HTTPError) and e.code == 404:
                if target.config.verbose:
                    error(f'    Resetting Git status file: {target.name}')
                d.reset_status(target.dep)

        return None


def unzip_and_load_target(target:BuildTarget, local_file:str) -> Tuple[bool, list]:
    success, num_extracted = try_unzip(local_file, target.dep.build_dir)
    if success:
        return artifactory_load_target(target, target.dep.build_dir, num_files_copied = num_extracted)
    else:
        error(f'    Artifactory unzip failed, possibly corrupt package {local_file}')
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

    archive = artifactory_archive_name(target)
    if not archive:
        return (False, None)
    
    cache_dir = target.dep.dep_dir #target.dep.workspace
    local_file = normalized_join(cache_dir, f'{archive}.zip')

    # cache is normally used, however `mama update` will ignore the cache and re-downloads the latest
    if os.path.exists(local_file) and not (target.config.update and target.is_current_target()):
        if (target.is_current_target() or target.config.no_specific_target()) \
            and not target.config.test:
            console(f'    Artifactory cache {local_file}')
        success, deps = unzip_and_load_target(target, local_file)
        if success: return (success, deps)

    # use mama verbose to show the failure msgs
    url = artifactory_sanitize_url(url)
    local_file = _fetch_package(target, url, archive, cache_dir)
    if not local_file:
        return (False, None)
    console(f'    Artifactory unzip {archive}')
    return unzip_and_load_target(target, local_file)

