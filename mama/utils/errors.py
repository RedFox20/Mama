"""The exceptions mama raises at a user, as opposed to a mamabuild bug."""


class BuildError(RuntimeError):
    """An expected failure the USER has to fix (a broken build, an unreachable repo), not a mamabuild
    bug. Reported as a clean message with no Python traceback - a stack trace through mama's internals
    only buries the actual compiler, cmake or git error the user needs to read."""


class GitError(BuildError):
    """A git command failed. The message is a full report: the cause, the url, the command mama ran and
    the git lines that name the failure. See types/git_errors.format_git_failure."""
