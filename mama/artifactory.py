from __future__ import annotations
import os, sys, ftplib, traceback, getpass
from typing import List, Tuple, TYPE_CHECKING

from . import build_names
from .mamafile_version import pinned_version
from .types.git import Git
from .types.local_source import LocalSource
from .types.artifactory_pkg import ArtifactoryPkg
from .types.dep_source import DepSource
from .types.asset import Asset
from .utils.system import Color, System, console, error, warning, progress
import mama.package as package
from .util import download_file, normalized_join, try_unzip, is_network_error
from .papa_deploy import PapaFileInfo


if TYPE_CHECKING:
    from .build_target import BuildTarget
    from .build_config import BuildConfig


class ArtifactoryCredentialsError(RuntimeError):
    pass


def artifactory_archive_name(target:BuildTarget):
    """
    Builds the archive name for a papa deploy package:
    {name}-{platform}-{os_major}-{compiler}-{arch}-{build_type}[-variant]-{version}
    The version is the first of: mamafile `self.version`, the pinned `git_tag`, or the commit hash.
    A `git_branch` pin labels the hash and does not replace it.
    target: the BuildTarget whose dep and config name the archive
    """
    p:ArtifactoryPkg = target.dep.dep_source

    if p.is_pkg and p.fullname:
        return p.fullname

    version = ''

    # a mamafile version wins over every dep source type and replaces the commit hash in the archive name
    if target.version:
        version = target.version
    else:
        if target.dep.is_root:
            version = Git.get_current_repository_commit(target.dep)
            if not version:
                return None # nothing to do at this point
        elif p.is_pkg:
            version = p.version
        elif p.is_git:
            git:Git = p
            # A tag is immutable by convention, so the tag alone names the package with no hash resolution.
            # add_git stores a git_commit pin in the tag field, and Git.is_hex_string routes it to the hash path below.
            version = '' if Git.is_hex_string(git.tag) else build_names.sanitize_version(git.tag)
            if not version:
                # No tag: the commit hash identifies the source, and a branch pin only prefixes it for a reader.
                # A branch moves, so its name alone would serve every commit ever pushed to it.
                commit = git.get_commit_hash(target.dep)
                if not commit:
                    return None # nothing to do at this point
                branch = build_names.sanitize_version(git.branch)
                version = f'{branch}-{commit}' if branch else commit
        elif p.is_src:
            if not version:
                raise RuntimeError(f'Local package {target.name} has no target.version set in mamafile')

    name = target.name
    # the triplet that makes the package name unique per platform
    platform, os_major, _ = target.config.get_distro_info()
    compiler = target.config.compiler_version()
    arch = target.config.arch # eg 'x86', 'arm64'
    # The SAME suffix the dep's build dir carries, read from the dep: it holds the pre-parse consumer args.
    # The pre-clone shim probe and the upload then compose the same name.
    build_type = ('release' if target.config.release else 'debug') + target.dep.variant_suffix

    return f'{name}-{platform}-{os_major}-{compiler}-{arch}-{build_type}-{version}'


keyr = None
def _get_keyring():
    global keyr
    if not keyr: # lazy init, because the keyring import loads certs and is slow
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
    username = os.getenv('MAMA_ARTIFACTORY_USER', None)
    password = os.getenv('MAMA_ARTIFACTORY_PASS', None)
    if username is not None:
        if not username:
            raise ArtifactoryCredentialsError(f'Artifactory Upload failed for {url}: missing username. ' \
                                              'Set MAMA_ARTIFACTORY_USER.')
        if not password:
            raise ArtifactoryCredentialsError(f'Artifactory Upload failed for {url}: missing password. ' \
                                              'Set MAMA_ARTIFACTORY_PASS.')
        return username, password

    if config.artifactory_auth == 'store':
        username = _get_keyring().get_password('mamabuild', f'username-{url}')
        password = _get_keyring().get_password('mamabuild', f'password-{url}')
        if username is not None and password is not None:
            return username, password

    if not sys.stdin.isatty():
        raise ArtifactoryCredentialsError(f'Artifactory Upload failed for {url}: missing credentials. ' \
                                          'Set MAMA_ARTIFACTORY_USER and MAMA_ARTIFACTORY_PASS.')

    try:
        username = input(f'{url} username: ').strip()
    except EOFError:
        raise ArtifactoryCredentialsError(f'Artifactory Upload failed for {url}: missing credentials. ' \
                                          'Set MAMA_ARTIFACTORY_USER and MAMA_ARTIFACTORY_PASS.') from None
    if not username:
        raise ArtifactoryCredentialsError(f'Artifactory Upload failed for {url}: missing username. ' \
                                          'Set MAMA_ARTIFACTORY_USER.')

    try:
        password = getpass.getpass(f'{username}@{url} password: ').strip()
    except EOFError:
        raise ArtifactoryCredentialsError(f'Artifactory Upload failed for {url}: missing password. ' \
                                          'Set MAMA_ARTIFACTORY_PASS.') from None
    if not password:
        raise ArtifactoryCredentialsError(f'Artifactory Upload failed for {url}: missing password. ' \
                                          'Set MAMA_ARTIFACTORY_PASS.')
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
    indent = f'  - {target_name: <16} '
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
                progress(f'{indent}|{left}>{right}| {percent:>3} %')
        progress(f'{indent}|>{" ":50}| {0:>3} %')  # via progress(), so a headless run throttles it
        try:
            ftp.cwd(target_name)
        except:
            ftp.mkd(target_name)
            ftp.cwd(target_name)
        ftp.storbinary(f'STOR {os.path.basename(file_path)}', f, callback=print_progress)
        progress(f'{indent}|{"="*50}>| 100 %', final=True)


def artifact_already_exists(ftp:ftplib.FTP_TLS, target:BuildTarget, file_path:str):
    items = []
    target_path = f'{target.name}/{os.path.basename(file_path)}'
    ftp.dir(target_path, items.append)
    if target.config.verbose:
        file_list = "\n    ".join(items)
        console(f'    Checking if artifact "{target_path}" already exists on server:\n    {file_list}')
    return len(items) > 0


def artifactory_upload_ftp(target:BuildTarget, file_path:str) -> bool:
    config = target.config
    url = config.artifactory_ftp
    if not url: raise RuntimeError(f'Artifactory Upload failed: artifactory_ftp not set by config.set_artifactory_ftp()')
    if config.verbose: console(f'  - Artifactory Upload {file_path}\n {"":12}-> {url}')

    with ftplib.FTP_TLS() as ftp:
        try:
            url = artifactory_sanitize_url(url)
            artifactory_ftp_login(ftp, config, url)
            if config.if_needed and artifact_already_exists(ftp, target, file_path):
                if config.print:
                    console(f'  - Artifactory Upload skipped: artifact already exists: {target.name}/{os.path.basename(file_path)}', color=Color.GREEN)
                return False # skip upload
            artifactory_upload(ftp, target.name, file_path)
            return True
        except ArtifactoryCredentialsError as e:
            error(str(e))
            raise SystemExit(-1)
        except:
            traceback.print_exc()
            raise SystemExit(-1)
        finally:
            if ftp.sock is not None:
                try:
                    ftp.quit()
                except Exception:
                    ftp.close()
    return False


def _warn_on_compiler_mismatch(target:BuildTarget, papa:PapaFileInfo):
    """A foreign-compiler package mixes libc++ archives into a libstdc++ build and fails the link.
    Compiler-scoped build dirs make this unreachable, so warn and do not fail. A pre-C-record package has no stamp."""
    if not papa.compiler: return  # pre-C-record package: unknown, allow
    try: current = target.config.compiler_version()
    except Exception: return
    if papa.compiler != current:
        warning(f'  - Target {target.name: <16} package was built with {papa.compiler}, this build uses {current}')


def artifactory_load_target(target:BuildTarget, deploy_path, num_files_copied) -> Tuple[bool, list]:
    """
    Reconfigures `target` from {deploy_path}/papa.txt. Returns (fetched:bool, dep_sources:list).
    target: the BuildTarget to fill with the papa.txt exports
    deploy_path: the directory that holds papa.txt
    num_files_copied: the extracted file count, shown by the verbose report
    """
    papa_list = normalized_join(deploy_path, 'papa.txt')
    if not os.path.exists(papa_list):
        error(f'    {target.name}  Artifactory Load failed because {papa_list} does not exist')
        return (False, None)

    if target.config.verbose:
        if num_files_copied != 0:
            console(f'    {target.name}  Artifactory Load ({num_files_copied} files were copied)', color=Color.RED)
        else:
            console(f'    {target.name}  Artifactory Load (no files modified)', color=Color.GREEN)

    papa = PapaFileInfo(papa_list)
    if papa.project_name != target.name:
        error(f'    {target.name}  Artifactory Load failed because {papa_list} ProjectName={papa.project_name} mismatches!')
        return (False, None)
    _warn_on_compiler_mismatch(target, papa)

    target.dep.from_artifactory = True
    target.exported_includes = papa.includes
    target.exported_assets = papa.assets
    package.set_export_libs_and_products(target, papa.libs)
    package.reload_syslibs(target, papa.syslibs)

    # save the commit hash status for a git dep, so the next fetch is faster
    if target.dep.dep_source.is_git:
        git: Git = target.dep.dep_source
        git.save_status(target.dep)

    return (True, papa.dependencies)


def _fetch_package(target:BuildTarget, url, archive, cache_dir):
    if not target.config.is_network_available():
        return None
    remote_file = f'http://{url}/{target.name}/{archive}.zip'
    try:
        return download_file(remote_file, cache_dir, force=True,
                             message=f'  - {target.name: <16} Artifactory fetch {url}/{archive} ',
                             name=target.name)
    except Exception as e:
        if is_network_error(e):
            target.config.mark_network_unavailable()
        if target.config.verbose or target.config.force_artifactory:
            error(f'    Artifactory fetch failed with {e} {url}/{archive}.zip')

        d:DepSource = target.dep.dep_source
        # this is an artifactory pkg, so the url MUST exist
        if d.is_pkg:
            raise RuntimeError(f'Artifactory package {d} did not exist at {url}')

        # A 404 for a git dep is NORMAL: no prebuilt archive exists for this commit. Do NOT wipe git_status.
        # check_status detects real SCM changes by direct comparison, and a wiped status forces a false full rebuild.

        return None


def unzip_and_load_target(target:BuildTarget, local_file:str) -> Tuple[bool, list]:
    success, num_extracted = try_unzip(local_file, target.dep.build_dir)
    if success:
        return artifactory_load_target(target, target.dep.build_dir, num_files_copied = num_extracted)
    else:
        error(f'    Artifactory unzip failed, possibly corrupt package {local_file}')
        os.remove(local_file)
        return (False, None)


def artifactory_fetch_and_reconfigure(target:BuildTarget) -> Tuple[bool, list]:
    """
    Tries to fetch a prebuilt package from artifactory. Returns (fetched:bool, dep_sources:list).
    target: the BuildTarget to load the package into
    """
    url = target.config.artifactory_ftp
    if not url:
        return (False, None)

    # A pinned version replaces the commit hash in the uploaded archive name, so the probe must use the pin.
    # A hash-named archive it finds instead can only be a stale pre-pin leftover.
    if not target.version:
        target.version = pinned_version(target.dep)

    archive = artifactory_archive_name(target)
    if not archive:
        return (False, None)

    cache_dir = target.dep.dep_dir
    local_file = normalized_join(cache_dir, f'{archive}.zip')

    # use the cache, except when `mama update` runs on this target: then download the latest
    if os.path.exists(local_file) and not (target.config.update and target.is_current_target()):
        if (target.is_current_target() or target.config.no_specific_target()) \
            and not target.config.test:
            console(f'    Artifactory cache {local_file}')
        success, deps = unzip_and_load_target(target, local_file)
        if success: return (success, deps)

    url = artifactory_sanitize_url(url)
    local_file = _fetch_package(target, url, archive, cache_dir)
    if not local_file:
        return (False, None)
    console(f'  - {target.name: <16} Artifactory unzip {archive}')
    return unzip_and_load_target(target, local_file)


def try_load_artifactory_shim(dep) -> Tuple:
    """
    Probes artifactory for a prebuilt package named by the commit hash that ls-remote resolves without a clone.
    On a hit, loads the papa.txt exports into a default BuildTarget, writes the shim marker, and
    returns (target, dep_sources). On a miss it leaves the dep untouched and returns (None, None),
    so the caller can use the clone path.
    dep: the git BuildDependency to probe
    """
    from .build_target import BuildTarget  # local import to avoid cycle

    config = dep.config
    if not config.artifactory_ftp:
        return (None, None)
    if not dep.dep_source.is_git:
        return (None, None)

    git: Git = dep.dep_source

    # resolve the commit hash without a clone, honoring the git_status cache when `update` is not set
    commit_hash = git.init_commit_hash(dep, use_cache=True, fetch_remote=True)
    if not commit_hash:
        if config.verbose:
            warning(f'    {dep.name}  shim probe: could not resolve commit hash')
        return (None, None)
    git.commit_hash = commit_hash  # cache for downstream consumers

    # first probe: version-named when a local mamafile pins self.version, else commit-hash-named
    probe_target = BuildTarget(name=dep.name, config=config, dep=dep, args=dep.target_args)
    fetched, dependencies = artifactory_fetch_and_reconfigure(probe_target)

    # Fallback: the dep may pin self.version in its own not-yet-cloned mamafile, so sparse-fetch only the
    # mamafile and re-probe with that version. A re-probe by hash after a version pin would resurrect a stale archive.
    if not fetched and not probe_target.version:
        version = git.fetch_self_version_from_remote(dep)
        if version:
            if config.verbose:
                warning(f'    {dep.name}  shim probe: retrying with self.version={version}')
            probe_target = BuildTarget(name=dep.name, config=config, dep=dep, args=dep.target_args)
            probe_target.version = version
            fetched, dependencies = artifactory_fetch_and_reconfigure(probe_target)

    if not fetched:
        # Reset any side effect on the dep so the clone path can run cleanly.
        dep.from_artifactory = False
        return (None, None)

    archive = artifactory_archive_name(probe_target)
    dep.write_shim_marker(archive_name=archive or '', commit_hash=commit_hash)
    config.update_stats.record_shim()
    if config.print:
        console(f'  - Target {dep.name: <16} SHIM FETCHED {archive}', color=Color.GREEN)

    return (probe_target, dependencies)
