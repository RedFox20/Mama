#!/bin/sh
# Usage: deploy.sh [--ci] [build|upload]. With no stage, it builds and then uploads.
# --ci: CI sets TWINE_PASSWORD from a secret, and twine must fail instead of prompting for it
upload_flags=
[ "$1" = "--ci" ] && { upload_flags="--non-interactive --disable-progress-bar"; shift; }
case "$1" in ""|build|upload) ;; *) echo "Usage: deploy.sh [--ci] [build|upload]" >&2; exit 2 ;; esac
if [ "$1" != "upload" ]; then
    python3 -m pip install -q -U build "twine>=6.1" "packaging>=24.2" && \
    python3 -m build --quiet && python3 -m twine check dist/* || exit 1
fi
[ "$1" = "build" ] && exit 0
if [ -n "$upload_flags" ] && [ -z "$TWINE_PASSWORD" ]; then
    echo "deploy.sh --ci: TWINE_PASSWORD is empty. Add the PYPI_API_TOKEN secret." >&2
    exit 1
fi
python3 -m twine upload --skip-existing $upload_flags dist/*
