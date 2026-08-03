#!/usr/bin/env bash
# Fingerprint of every pending change: tracked edits plus the content of untracked files. A passing
# review records this value, and the Stop hook stays quiet while it still matches.
root=$(git rev-parse --show-toplevel 2>/dev/null) || exit 0
cd "$root" || exit 0
{ git diff HEAD; git ls-files --others --exclude-standard -z | xargs -0 cat 2>/dev/null; } \
  | sha1sum | cut -d' ' -f1
