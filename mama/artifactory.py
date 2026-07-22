from __future__ import annotations
import os, sys, ftplib, traceback, getpass
from typing import List, Tuple, TYPE_CHECKING

from .types.git import Git
from .types.local_source import LocalSource
from .types.artifactory_pkg import ArtifactoryPkg
from .types.dep_source import DepSource
from .types.asset import Asset
from .utils.system import Color, System, console, error, warning, progress
import mama.package as package
from .util import download_file, normalized_join, try_unzip, is_network_error, read_text_from
from .papa_deploy import PapaFileInfo


if TYPE_CHECKING:
    from .build_target import BuildTarget
    from .build_config import BuildConfig


class ArtifactoryCredentialsError(RuntimeError):
    pass


def artifactory_archive_name(target:BuildTarget):
    """
    Constructs archive name for papa deploy packages in the form of:
    {name}-{platform}-{compiler}-{arch}-{build_type}-{commit_hash}
    Example: opencv-linux-x64-gcc9-release-df76b66
    """
    p:ArtifactoryPkg = target.dep.dep_source

    # if this is an ArtifactoryPkg with full name of the archive
    if p.is_pkg and p.fullname:
        return p.fullname

    version = ''

    # if mamafile defines a specific version tag, then we will respect that
    # regardless of dependency source type or any commit hashes
    # explicit versioning will remove the version hash tag from the archive name
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
            version = git.get_commit_hash(target.dep)
            if not version:
                return None # nothing to do at this point
        elif p.is_src:
            if not version:
                raise RuntimeError(f'Local package {target.name} has no target.version set in mamafile')

    name = target.name
    # triplets information to make this package platform unique
    platform, os_major, _ = target.config.get_distro_info()
    compiler = target.config.compiler_version()
    arch = target.config.arch # eg 'x86', 'arm64'
    build_type = 'release' if target.config.release else 'debug'
    sanitizer_suffix = target.config.sanitizer_suffix()
    # e.g. appends "-asan_tsan" if both of them are enabled
    if sanitizer_suffix:
        build_type += '-' + sanitizer_suffix

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
        console(f'{indent}|>{" ":50}| {0:>3} %', end='')
        # chdir into FTP_ROOT/target_name/
        try:
            ftp.cwd(target_name)
        except:
            ftp.mkd(target_name) # create subdirectory if needed
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
    """Foreign-compiler package = libc++ archives in a libstdc++ build, dies on undefined std::__1:: symbols.
    Compiler-scoped build dirs make this unreachable, so warn (don't fail) - a pre-C-record package has no stamp."""
    if not papa.compiler: return  # pre-C-record package: unknown, allow
    try: current = target.config.compiler_version()
    except Exception: return
    if papa.compiler != current:
        warning(f'  - Target {target.name: <16} package was built with {papa.compiler}, this build uses {current}')


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
            console(f'    {target.name}  Artifactory Load ({num_files_copied} files were copied)', color=Color.RED)
        else:
            console(f'    {target.name}  Artifactory Load (no files modified)', color=Color.GREEN)

    papa = PapaFileInfo(papa_list)
    if papa.project_name != target.name:
        error(f'    {target.name}  Artifactory Load failed because {papa_list} ProjectName={papa.project_name} mismatches!')
        return (False, None)
    _warn_on_compiler_mismatch(target, papa)

    target.dep.from_artifactory = True
    target.exported_includes = papa.includes # include folders to export from this target
    target.exported_assets = papa.assets # exported asset files
    package.set_export_libs_and_products(target, papa.libs)
    package.reload_syslibs(target, papa.syslibs) # set exported system libraries

    # for git repos, save the used commit hash status, so that next time the fetch is faster
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

        # NB: a 404 here for a git dep is normal (no prebuilt archive uploaded
        # for the current commit). DO NOT wipe git_status - check_status already
        # detects url/tag/branch/commit changes from the mamafile; wiping the
        # status causes the *next* `mama update` to falsely report 'SCM change
        # detected' and trigger a full rebuild of an already-up-to-date dep.

        return None


def unzip_and_load_target(target:BuildTarget, local_file:str) -> Tuple[bool, list]:
    success, num_extracted = try_unzip(local_file, target.dep.build_dir)
    if success:
        return artifactory_load_target(target, target.dep.build_dir, num_files_copied = num_extracted)
    else:
        error(f'    Artifactory unzip failed, possibly corrupt package {local_file}')
        os.remove(local_file) # it's probably corrupted
        return (False, None)


def resolve_pinned_version(dep) -> str:
    """A `self.version = '<literal>'` pinned in the dep's mamafile, read from disk WITHOUT
    executing it (mamafiles typically set version inside configure(), which never runs on
    download probes - only on the upload side, where it renames the archive). Pre-clone the
    mamafile is on disk only for a parent-repo override (dep.mamafile); post-clone also in
    the dep's own tree. Returns '' when unpinned or the mamafile isn't on disk yet."""
    path = dep.mamafile_path()
    if path and os.path.exists(path):
        try:
            return Git.extract_self_version(read_text_from(path)) or ''
        except OSError:
            return ''
    return ''


def artifactory_fetch_and_reconfigure(target:BuildTarget) -> Tuple[bool, list]:
    """
    Try to fetch prebuilt package from artifactory
    Returns (fetched:bool, dep_sources:list)
    """
    url = target.config.artifactory_ftp
    if not url:
        return (False, None)

    # A pinned version names the UPLOADED archive (artifactory_archive_name drops the commit
    # hash), so a probe without it looks for a name uploads no longer produce - and a
    # hash-named archive it finds instead can only be a stale pre-pin leftover.
    if not target.version:
        target.version = resolve_pinned_version(target.dep)

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
    console(f'  - {target.name: <16} Artifactory unzip {archive}')
    return unzip_and_load_target(target, local_file)


def try_load_artifactory_shim(dep) -> Tuple:
    """
    Probe artifactory for a prebuilt package using the commit hash resolved via
    `git ls-remote` (no clone). On hit, construct a default BuildTarget, load
    papa.txt exports/deps into it, write the shim marker, and return the target
    plus its child dep_sources.

    On miss (or when artifactory is not configured), returns (None, None) and
    leaves dep state untouched so the caller can fall back to the clone path.

    Returns (target_or_None, dep_sources_or_None).
    """
    from .build_target import BuildTarget  # local import to avoid cycle

    config = dep.config
    if not config.artifactory_ftp:
        return (None, None)
    if not dep.dep_source.is_git:
        return (None, None)

    git: Git = dep.dep_source

    # Resolve commit hash without cloning. `init_commit_hash` already supports
    # ls-remote and respects the stored git_status cache when `update` is not set.
    commit_hash = git.init_commit_hash(dep, use_cache=True, fetch_remote=True)
    if not commit_hash:
        if config.verbose:
            warning(f'    {dep.name}  shim probe: could not resolve commit hash')
        return (None, None)
    git.commit_hash = commit_hash  # cache for downstream consumers

    # First probe: version-named if a local mamafile pins self.version (fetch_and_reconfigure
    # resolves it), else commit-hash-named. Works for the common case.
    probe_target = BuildTarget(name=dep.name, config=config, dep=dep, args=dep.target_args)
    fetched, dependencies = artifactory_fetch_and_reconfigure(probe_target)

    # Fallback: dep may pin target.version (e.g. boost 1.60) in its own not-yet-cloned
    # mamafile, so its archive name doesn't include the commit hash. Sparse-fetch only the
    # mamafile, grep self.version, and re-probe with that version. A version-pinned first
    # probe gets no fallback: re-probing by hash would resurrect a stale pre-pin archive.
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

    # Hit: persist marker and return the configured target.
    archive = artifactory_archive_name(probe_target)
    dep.write_shim_marker(archive_name=archive or '', commit_hash=commit_hash)
    config.update_stats.record_shim()
    if config.print:
        console(f'  - Target {dep.name: <16} SHIM FETCHED {archive}', color=Color.GREEN)

    return (probe_target, dependencies)
