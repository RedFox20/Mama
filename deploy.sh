#!/bin/sh
# Usage: deploy.sh [--ci] [build|upload]. With no stage, it builds and then uploads.
# --ci: CI sets TWINE_PASSWORD from a secret, and twine must fail instead of prompting for it
[ "$1" = "--ci" ] && { ci=1; shift; }
if [ "$1" != "upload" ]; then
    python3 -m pip install -q -U build "twine>=6.1" "packaging>=24.2" && \
    python3 -m build --quiet && python3 -m twine check dist/* || exit 1
fi
[ "$1" = "build" ] && exit 0
if [ -n "$ci" ]; then
    [ -n "$TWINE_PASSWORD" ] || { echo "deploy.sh --ci: TWINE_PASSWORD is empty. Add the PYPI_API_TOKEN secret." >&2; exit 1; }
    upload_flags="--non-interactive --disable-progress-bar"
fi
python3 -m twine upload --skip-existing $upload_flags dist/*
