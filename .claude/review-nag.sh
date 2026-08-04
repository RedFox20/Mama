#!/usr/bin/env bash
# Stop hook: ask for a style review ONCE per distinct diff, then stay quiet.
#
# Three files gate it, all gitignored:
#   .claude/.planning       while it exists, the session is planning and writes no code. Never ask.
#   .claude/.review-passed  the diff a review already approved. Never ask again for that one.
#   .claude/.review-nagged  the diff this hook last asked about. Asking twice for it helps nobody,
#                           and it used to fire on every stop, which blocked the user from typing.
#
# The reminder returns as soon as the diff changes, which is the only time it carries new information.
set -u
cd "$(git rev-parse --show-toplevel 2>/dev/null)" || exit 0
[ -f .claude/.planning ] && exit 0
git status --porcelain 2>/dev/null | grep -q . || exit 0   # a clean tree has nothing to review

# stderr is silenced on purpose: git warns about CRLF on Windows, and only stdout carries the hook reply
hash=$(bash .claude/review-hash.sh 2>/dev/null)
[ "$hash" = "$(cat .claude/.review-passed 2>/dev/null)" ] && exit 0
[ "$hash" = "$(cat .claude/.review-nagged 2>/dev/null)" ] && exit 0
printf '%s' "$hash" > .claude/.review-nagged

printf '%s' '{"systemMessage":"Changes since the last passing review: run /mama-style-review before you finish.","hookSpecificOutput":{"hookEventName":"Stop","additionalContext":"The tree changed since the last passing review. Run the mama-style-review skill: step 0 re-reads .claude/skills/ste-writing/SKILL.md and .claude/skills/output-style/SKILL.md, because both rule sets drift out of a long session. The review records .claude/.review-passed when it reports 0 issues. This reminder asks once per diff, so it stays quiet until the diff changes again."}}'
