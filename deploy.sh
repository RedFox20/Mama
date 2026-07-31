python3 -m pip install -U build "twine>=6.1" "packaging>=24.2" && \
python3 -m build && python3 -m twine check dist/* && python3 -m twine upload --skip-existing dist/*
