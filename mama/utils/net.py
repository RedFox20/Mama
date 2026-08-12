"""Download a file, and tell a network failure apart from an auth failure. ssl and urllib cost about
26ms to import, so both stay inside the function that needs them. See tests/test_import_cost/."""

import os
from typing import Tuple

from .errors import BuildError
from .system import console, error
from .paths import normalized_join
from .progress import ProgressBar, get_file_size_str
from .archive import unzip

# Seconds of silence from the server that end a download. The socket restarts the clock on every read,
# so a slow but live transfer never trips it. A dead network costs one wait per download.
DOWNLOAD_TIMEOUT = 5           # a download mama can answer another way, such as an artifactory fetch
REQUIRED_DOWNLOAD_TIMEOUT = 15 # a file the build cannot continue without, such as a source archive


class DownloadError:
    """Why one download failed. A download reports the failure as data, so the caller decides what to
    do. It can use a cached copy, mark the network unavailable, or stop the build."""
    def __init__(self, url:str, reason:str, status=0, network=False):
        self.url = url
        self.reason = reason
        self.status = status    # the HTTP status code, 0 when the request never got a response
        self.network = network  # True when the network failed: DNS, refused, reset or timed out

    def __str__(self):
        return f'Failed to download {self.url}: {self.reason}'


def _download_error(url:str, e:Exception, timeout:int) -> DownloadError:
    """Turns a urllib, ssl or socket exception into one line the user can act on."""
    import socket
    from urllib.error import HTTPError
    if isinstance(e, HTTPError):
        return DownloadError(url, f'HTTP {e.code} {e.reason}', status=e.code, network=False)
    reason = getattr(e, 'reason', None) or e  # URLError wraps the real cause in .reason
    if isinstance(reason, (TimeoutError, socket.timeout)):
        text = f'the server sent no data for {timeout} seconds'
    elif isinstance(reason, socket.gaierror):
        text = 'the host name does not resolve'
    else:
        text = f'{type(reason).__name__}: {reason}'
    return DownloadError(url, text, network=is_network_error(e))


def _transfer_body(urlfile, local_file:str, size:int, indent:str) -> int:
    """Streams the body into a partial file and returns the transferred byte count. The partial file
    replaces local_file only after a complete transfer. Every other end deletes it, so a failure and a
    stop signal both leave the cached file the transfer never opened."""
    part_file = local_file + '.part'
    bar = ProgressBar(size, indent)
    transferred = 0
    try:
        with open(part_file, 'wb') as output:
            while transferred < size:
                data = urlfile.read(32*1024)
                if not data: break
                output.write(data)
                transferred += len(data)
                bar.step(len(data))
        if transferred >= size: os.replace(part_file, local_file)  # atomic on POSIX and on Windows
    finally:
        if os.path.exists(part_file): os.remove(part_file)
    bar.finish()
    return transferred


def try_download_file(remote_url:str, local_dir:str, force=False, message=None, name:str=None,
                      timeout:int=DOWNLOAD_TIMEOUT) -> Tuple[str, DownloadError]:
    """Downloads remote_url into local_dir. Returns (local_file, None) after a complete transfer, and
    (None, DownloadError) after any failure. It raises nothing, so the caller reports what failed.
    - force: [False] use any existing local file without contacting the server. When True, open the
      connection and compare Content-Length, and skip the body transfer when the sizes match.
    - message: [None] custom log line for the download start
    - name: [None] target name that prefixes the log lines under parallel updates
    - timeout: [DOWNLOAD_TIMEOUT] seconds of server silence that end the download. Pass
      REQUIRED_DOWNLOAD_TIMEOUT for a file no cache can replace."""
    import ssl  # deferred: only a download needs it
    from urllib import request
    local_file = normalized_join(local_dir, os.path.basename(remote_url))
    indent = f'  - {name: <16} ' if name else '    '
    if not force and os.path.exists(local_file):
        console(f'{indent}Using locally cached {local_file}')
        return local_file, None
    if not os.path.exists(local_dir):
        os.makedirs(local_dir, exist_ok=True)

    # TODO: this causes issues inside some secure networks
    if remote_url.startswith('https://'):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_OPTIONAL
    else:
        ctx = None

    try:
        with request.urlopen(remote_url, context=ctx, timeout=timeout) as urlfile:
            size = urlfile.info()['Content-Length']
            size = int(size.strip()) if size else None

            # size-match cache: one HTTP round-trip, already paid by opening the connection, saves the whole body
            if size is not None and os.path.exists(local_file) \
                    and os.path.getsize(local_file) == size:
                console(f'{indent}Artifactory CACHE (size-match) '
                        f'{os.path.basename(local_file)} ({get_file_size_str(size)})')
                return local_file, None

            if not message: message = f'Downloading {remote_url}'
            if not size:
                console(f'{message} unknown size')
                return None, DownloadError(remote_url, 'the server sent no Content-Length header')

            want = get_file_size_str(size)
            console(f'{message} {want}')
            transferred = _transfer_body(urlfile, local_file, size, indent)
    except Exception as e:
        return None, _download_error(remote_url, e, timeout)

    if transferred < size:
        return None, DownloadError(remote_url, f'the transfer stopped at {get_file_size_str(transferred)} of {want}')
    return local_file, None


def download_file(remote_url:str, local_dir:str, force=False, message=None, name:str=None,
                  timeout:int=DOWNLOAD_TIMEOUT) -> str:
    """Downloads remote_url into local_dir and returns the local file path. It raises BuildError with
    the url and the reason when the download fails. A caller that handles a failure itself calls
    try_download_file, which returns (local_file, error) and raises nothing.
    See try_download_file for the arguments."""
    local_file, err = try_download_file(remote_url, local_dir, force, message, name, timeout)
    if err: raise BuildError(str(err))
    return local_file


def download_and_unzip(remote_file, extract_dir, local_file, timeout:int=DOWNLOAD_TIMEOUT):
    if local_file and os.path.exists(local_file):
        console(f"Skipping {os.path.basename(remote_file)} because {local_file} exists.")
        return extract_dir
    local_file, err = try_download_file(remote_file, extract_dir, timeout=timeout)
    if err:
        error(f'    {err}')
        return None
    unzip(local_file, extract_dir)
    console(f'Extracted {local_file} to {extract_dir}')
    return extract_dir


def is_network_error(e: Exception) -> bool:
    """True only if the exception clearly indicates network unavailability: DNS failure, connection
    refused or reset, timeout. False for auth errors (SSH key rejected, HTTP 401/403), HTTP 404,
    and anything ambiguous."""
    import subprocess, socket
    from urllib.error import HTTPError, URLError

    if isinstance(e, subprocess.TimeoutExpired):
        return True
    if isinstance(e, HTTPError):
        return False
    if isinstance(e, URLError):
        reason = getattr(e, 'reason', None)
        if isinstance(reason, (socket.timeout, socket.gaierror,
                               ConnectionRefusedError, ConnectionResetError,
                               TimeoutError, OSError)):
            return True
        return not isinstance(reason, str)
    if isinstance(e, (ConnectionRefusedError, ConnectionResetError,
                      TimeoutError, socket.timeout, socket.gaierror)):
        return True
    if isinstance(e, OSError):
        import errno
        if e.errno in (errno.ENETUNREACH, errno.EHOSTUNREACH,
                       errno.ECONNREFUSED, errno.ETIMEDOUT, errno.ECONNRESET):
            return True

    msg = str(e).lower()
    auth_patterns = [
        'permission denied', 'authentication failed',
        'host key verification failed',
        'returned error: 401', 'returned error: 403',
        'invalid credentials',
    ]
    for p in auth_patterns:
        if p in msg:
            return False
    network_patterns = [
        'could not resolve host', 'connection refused',
        'connection timed out', 'network is unreachable',
        'no route to host', 'name or service not known',
        'temporary failure in name resolution', 'connection reset',
    ]
    for p in network_patterns:
        if p in msg:
            return True
    return False
