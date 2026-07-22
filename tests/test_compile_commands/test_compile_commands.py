"""Pins that sanitized/coverage builds don't repoint c_cpp_properties.json compileCommands."""
import json, os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from mama.dependency_chain import _save_vscode_compile_commands, _find_matching_platform_config


def _make_vscode_dep(tmp_path, sanitize=None, coverage=None):
    src = tmp_path / 'proj'
    (src / '.vscode').mkdir(parents=True)
    (src / 'linux').mkdir()
    (src / 'linux' / 'compile_commands.json').write_text('[]')
    props = {"configurations": [{"name": "Linux", "compileCommands": "ORIGINAL"}]}
    props_path = src / '.vscode' / 'c_cpp_properties.json'
    props_path.write_text(json.dumps(props, indent=4))

    cfg = Mock(); cfg.sanitize = sanitize; cfg.coverage = coverage
    cfg.print = False; cfg.name.return_value = 'linux'; cfg.arch = 'x64'
    dep = Mock(); dep.src_dir = str(src); dep.build_dir = f'{src}/linux'
    dep.is_root = True; dep.config = cfg
    return dep, props_path


def _commands(props_path):
    return json.loads(props_path.read_text())["configurations"][0]["compileCommands"]


def test_plain_build_updates_compile_commands(tmp_path):
    dep, props_path = _make_vscode_dep(tmp_path)
    _save_vscode_compile_commands(dep)
    assert _commands(props_path) != 'ORIGINAL'


def test_sanitized_build_leaves_compile_commands_untouched(tmp_path):
    dep, props_path = _make_vscode_dep(tmp_path, sanitize='address')
    _save_vscode_compile_commands(dep)
    assert _commands(props_path) == 'ORIGINAL'


def test_coverage_build_leaves_compile_commands_untouched(tmp_path):
    dep, props_path = _make_vscode_dep(tmp_path, coverage='gcov')
    _save_vscode_compile_commands(dep)
    assert _commands(props_path) == 'ORIGINAL'


def _cfg_dep(clang: bool):
    cfg = SimpleNamespace(clang=clang, gcc=not clang, arch='x64', name=lambda: 'linux')
    return SimpleNamespace(config=cfg)


def test_a_config_named_for_the_other_compiler_is_never_picked():
    confs = [{'name': 'Linux GCC x64'}, {'name': 'Linux Clang x64'}]
    assert _find_matching_platform_config(_cfg_dep(clang=True), confs)['name'] == 'Linux Clang x64'
    assert _find_matching_platform_config(_cfg_dep(clang=False), confs)['name'] == 'Linux GCC x64'


def test_a_compiler_agnostic_config_still_matches():
    confs = [{'name': 'Linux x64'}]
    assert _find_matching_platform_config(_cfg_dep(clang=True), confs)['name'] == 'Linux x64'
    assert _find_matching_platform_config(_cfg_dep(clang=True), [{'name': 'Windows x64'}]) is None
