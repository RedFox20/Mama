"""Pins that platform facts and build-system options stay in separate layers."""
import ast
import os
import pytest

from testutils import grep_mama_sources, CMAKE_OPTIONS, MAMA_DIR
from mama.platforms.registry import PLATFORMS

_PLATFORMS_DIR = os.path.join(MAMA_DIR, 'platforms')


def _platform_modules():
    return [f for f in sorted(os.listdir(_PLATFORMS_DIR)) if f.endswith('.py')]


def _source(module_file) -> str:
    with open(os.path.join(_PLATFORMS_DIR, module_file), encoding='utf-8') as f:
        return f.read()


def _imports(module_file):
    tree = ast.parse(_source(module_file))
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return names


@pytest.mark.parametrize('module_file', _platform_modules())
def test_no_platform_imports_a_build_system(module_file):
    """A platform describes its toolchain. mama/buildsys renders it. Import the renderer here and a
    second build system can never be added without rewriting all eleven platforms."""
    leaks = [name for name in _imports(module_file) if name.startswith('mama.buildsys')]
    assert leaks == [], f'platforms/{module_file} imports {leaks}'


# Toolchain.extra_opts is the documented escape hatch for options only one build system and one
# platform understand. iOS is its only user: Xcode SDK selection has no neutral form.
_ESCAPE_HATCH = 'ios.py'


@pytest.mark.parametrize('module_file', [f for f in _platform_modules() if f != _ESCAPE_HATCH])
def test_no_platform_names_a_cmake_variable(module_file):
    """A platform describes a toolchain, it does not name build-system variables. Everything that
    used to be a CMAKE_ string here is a Toolchain field now."""
    lines = [f'{module_file}:{n}' for n, line in enumerate(_source(module_file).splitlines(), 1)
             if 'CMAKE_' in line]
    assert lines == [], f'name it as a Toolchain field, not a cmake variable: {lines}'


@pytest.mark.parametrize('platform_class', PLATFORMS, ids=lambda p: p.name)
def test_no_platform_builds_its_own_option_list(platform_class):
    """The option list used to live on each platform, so adding one could forget CMAKE_SYSTEM_PROCESSOR
    or the toolchain-file recording. Now there is one renderer and nothing to forget."""
    assert not hasattr(platform_class, 'get_cmake_build_opts')


def test_only_the_renderer_names_the_target_system():
    """Setting CMAKE_SYSTEM_NAME anywhere else leaves the processor to the toolchain file - and a seeded
    build skips system determination, so the toolchain file never runs and the host value survives."""
    inline = grep_mama_sources(("'CMAKE_SYSTEM_NAME=", '"CMAKE_SYSTEM_NAME=', 'CMAKE_SYSTEM_NAME={'),
                               skip=(CMAKE_OPTIONS,))
    assert inline == [], f'emit it through mama/{CMAKE_OPTIONS}, not inline: {inline}'


def test_only_the_renderer_formats_the_toolchain_file_option():
    """Formatting it anywhere else leaves cmake_toolchain_file unset, and mama would pass
    CMAKE_C_COMPILER again - the exact bug that made a seeded android build re-detect as the host."""
    inline = grep_mama_sources(('CMAKE_TOOLCHAIN_FILE="',), skip=(CMAKE_OPTIONS,))
    assert inline == [], f'format it via use_toolchain_file(), not inline: {inline}'


def test_every_cmake_module_lives_under_buildsys():
    """One build system, one package. cmake logic scattered at the top level is what made a platform
    reach for it in the first place."""
    stray = [f'{root}/{f}' for root, _, files in os.walk(MAMA_DIR) for f in files
             if 'cmake' in f and f.endswith('.py') and 'buildsys' not in root]
    assert stray == []
