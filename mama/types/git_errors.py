"""Name the cause of a failed git command and render it as a report the user can act on.
One table drives both the retry decision and the report, so a new pattern is one line in one place."""

from typing import NamedTuple

_STALL_MARKER = 'git stalled'  # what mama itself writes when its idle timeout kills a hung git


class GitFailure(NamedTuple):
    transient: bool  # True when another attempt can succeed
    reason: str
    hint: str


_RETRY_HINT = 'Check the network, the vpn and the proxy, then run the command again.'
_WAIT_HINT = 'Wait a minute, then run the command again.'
_URL_HINT = 'Check the git url in the mamafile.'
_REF_HINT = 'Check the branch, tag or commit in the mamafile.'
_KEY_HINT = 'Check the ssh key of this machine, or the access token of the repository.'
_WIPE_HINT = 'Run `mama wipe <target>` to clone the target again.'
_UNKNOWN_HINT = 'Run mama with `verbose` to see the full git output.'

# (needles, transient, reason, hint). The scan returns the FIRST match, so each specific cause comes
# before the general one: a rate limit answers 403, and a 404 over https also prints 'unable to access'.
_CAUSES = (
    (('too many requests', 'rate limit', 'error: 429', 'try again later', 'temporarily unavailable'), True,
     'the server throttled mama (rate limit)', _WAIT_HINT),
    (('error: 500', 'error: 502', 'error: 503', 'internal server error'), True,
     'the server returned a temporary error', _WAIT_HINT),
    (('repository not found', 'error: 404', 'does not appear to be a git repository', 'does not exist'), False,
     'the repository does not exist, or this machine cannot see it', _URL_HINT),
    (('permission denied', 'access denied', 'error: 403'), False,
     'the server denied access to the repository', _KEY_HINT),
    (('authentication failed', 'invalid username or password', 'could not read username'), False,
     'authentication failed', 'Check the credentials for this host.'),
    (('host key verification failed',), False, 'the host key of the server is not trusted',
     'Add the host to ~/.ssh/known_hosts with `ssh-keyscan <host>`.'),
    (("couldn't find remote ref", 'not found in upstream', 'unknown revision', 'reference is not a tree',
      'did not match any file'), False, 'the branch, tag or commit does not exist on the remote', _REF_HINT),
    (('already exists and is not an empty directory',), False, 'the clone directory is not empty', _WIPE_HINT),
    (('no space left on device', 'disk quota exceeded'), False, 'the disk is full', 'Free some disk space.'),
    (('not a git repository',), False, 'the directory is not a git repository', _WIPE_HINT),
    ((_STALL_MARKER,), True, 'git sent no output before the idle timeout, so mama killed it',
     'The server hung, or git waited for a passphrase. Load the ssh key, then run the command again.'),
    (('connection reset', 'rpc failed', 'early eof', 'remote end hung up', 'connection closed by remote host',
      'kex_exchange_identification', 'ssh_exchange_identification', 'session request failed', 'broken pipe'), True,
     'the remote closed the connection during the transfer', _RETRY_HINT),
    (('timed out',), True, 'the connection timed out', _RETRY_HINT),
    (('could not resolve host',), True, 'the host name of the url does not resolve',
     'Check the network and the host name in the git url.'),
    (('failed to connect to', 'network is unreachable', 'no route to host', 'unable to access'), True,
     'mama cannot reach the git server', _RETRY_HINT),
)

# Substrings that mark the line naming the failure. The rest of the output is progress and transfer text.
_ERROR_NEEDLES = ('fatal:', 'error:', 'denied', 'ssh:', 'warning:', '[mama]')
_MAX_GIT_LINES = 6  # enough for the git error and the two lines after it, short enough to read
_TAIL_LINES = 3     # fallback when no line matches: each git version words an error differently


def stall_message(timeout) -> str:
    """What mama appends to the git output when its idle timeout kills a hung git. Mama writes it into
    the output, and does not only print it, so the report and the retry decision both read it."""
    return f'[mama] {_STALL_MARKER} {timeout}s, killed (auth prompt or hung server)'


def classify_git_failure(output: str, exit_code=0) -> GitFailure:
    """Name the cause of a failed git command from its output."""
    low = output.lower()
    for needles, transient, reason, hint in _CAUSES:
        if any(n in low for n in needles): return GitFailure(transient, reason, hint)
    return GitFailure(False, f'git exited with code {exit_code}', _UNKNOWN_HINT)


def git_error_lines(output: str) -> list:
    """The lines of the git output that name the failure, without the progress and transfer text."""
    lines = list(filter(None, (line.strip() for line in output.splitlines())))  # one strip per line, not two
    named = [line for line in lines if any(n in line.lower() for n in _ERROR_NEEDLES)]
    return (named or lines[-_TAIL_LINES:])[:_MAX_GIT_LINES]


def format_git_failure(headline: str, fields: dict, output: str, cause: GitFailure) -> str:
    """Render a failed git command as one block: the cause, then the command mama ran, then the git
    lines that name the failure. Every line joins with '\\n  ', so the block keeps the caller's indent."""
    lines = [headline, f'  {"reason": <8} {cause.reason}']
    lines += [f'  {label: <8} {value}' for label, value in fields.items() if value]
    lines += [f'  {("git" if i == 0 else ""): <8} {line}' for i, line in enumerate(git_error_lines(output))]
    if cause.hint: lines.append(f'  {"hint": <8} {cause.hint}')
    return '\n  '.join(lines)
