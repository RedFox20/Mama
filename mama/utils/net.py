"""Download a file, and tell a network failure apart from an auth failure. ssl and urllib cost about
26ms to import, so both stay inside the function that needs them. See tests/test_import_cost/."""

import os

from .system import console
from .paths import normalized_join
from .progress import ProgressBar, get_file_size_str
from .archive import unzip


def download_file(remote_url:str, local_dir:str, force=False, message=None, name:str=None):
    """Downloads remote_url into local_dir. Returns the local file path, or None on failure.
    - force: [False] use any existing local file without contacting the server. When True, open the
      connection and compare Content-Length, and skip the body transfer when the sizes match.
    - message: [None] custom log line for the download start
    - name: [None] target name that prefixes the log lines under parallel updates"""
    import ssl  # deferred: only a download needs it
    from urllib import request
    local_file = normalized_join(local_dir, os.path.basename(remote_url))
    indent = f'  - {name: <16} ' if name else '    '
    if not force and os.path.exists(local_file):
        console(f'{indent}Using locally cached {local_file}')
        return local_file
    if not os.path.exists(local_dir):
        os.makedirs(local_dir, exist_ok=True)

    # TODO: this causes issues inside some secure networks
    if remote_url.startswith('https://'):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_OPTIONAL
    else:
        ctx = None

    with request.urlopen(remote_url, context=ctx, timeout=5) as urlfile:
        size = urlfile.info()['Content-Length']
        size = int(size.strip()) if size else None

        # size-match cache: one HTTP round-trip, already paid by opening the connection, saves the whole body
        if size is not None and os.path.exists(local_file) \
                and os.path.getsize(local_file) == size:
            console(f'{indent}Artifactory CACHE (size-match) '
                    f'{os.path.basename(local_file)} ({get_file_size_str(size)})')
            return local_file

        if not message: message = f'Downloading {remote_url}'
        console(f'{message} {get_file_size_str(size) if size else "unknown size"}')
        if not size:
            return None

        bar = ProgressBar(size, indent)
        transferred = 0
        with open(local_file, 'wb') as output:
            while transferred < size:
                data = urlfile.read(32*1024)
                if not data: break
                output.write(data)
                transferred += len(data)
                bar.step(len(data))

    bar.finish()
    return local_file


def download_and_unzip(remote_file, extract_dir, local_file):
    if local_file and os.path.exists(local_file):
        console(f"Skipping {os.path.basename(remote_file)} because {local_file} exists.")
        return extract_dir
    local_file = download_file(remote_file, extract_dir)
    if not local_file:
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
