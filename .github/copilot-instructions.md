# Mama Build Tool - Project Guidelines

## Documentation

When modifying any user-facing behavior (CLI arguments, commands, flags, configuration options, mamafile API), always update **all** of the following to stay in sync:

1. **`mama/main.py`** — the `print_usage()` function that displays CLI help
2. **`README.md`** — the user-facing documentation
3. **`mama/build_config.py`** — the `parse_args()` method that parses CLI arguments

A change to any one of these three files should prompt a review of the other two.

## CLI Help Consistency Check

Before finishing any task that touches `mama/main.py` or `mama/build_config.py`, verify that:

- Every argument handled in `BuildConfig.parse_args()` has a corresponding entry in `print_usage()`
- Every entry in `print_usage()` is actually handled in `parse_args()`
- Descriptions in `print_usage()` match the actual behavior implemented in `build_config.py`
- The README.md command/flag reference matches both files

Flag any discrepancies found and fix them as part of the change.

## Project Structure

- `mama/` — main package (Python CLI tool for C++ cross-platform builds)
- `mama/platforms/` — cross-compilation platform definitions (Android, Yocto variants, MIPS, etc.)
- `mama/types/` — dependency source types (git, local, artifactory)
- `mama/utils/` — utility modules (subprocess, system helpers, gdb, gtest)
- `tests/` — integration tests with example projects
