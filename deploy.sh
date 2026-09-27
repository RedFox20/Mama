# --ci: CI sets TWINE_PASSWORD from a secret, and twine must fail instead of prompting for it
if [ "$1" = "--ci" ]; then
    [ -n "$TWINE_PASSWORD" ] || { echo "deploy.sh --ci: TWINE_PASSWORD is empty. Add the PYPI_API_TOKEN secret." >&2; exit 1; }
    upload_flags=--non-interactive
fi
python3 -m pip install -U build "twine>=6.1" "packaging>=24.2" && \
python3 -m build && python3 -m twine check dist/* && python3 -m twine upload --skip-existing $upload_flags dist/*
