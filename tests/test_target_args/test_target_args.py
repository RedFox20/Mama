"""Pins unused-arg handling: a bare word becomes the target, an option-shaped typo fails immediately."""
from types import SimpleNamespace
from unittest.mock import Mock, patch
import pytest
from mama.main import set_target_from_unused_args, check_config_target


def _mk(unused, target):
    cfg = Mock()
    cfg.unused_args = list(unused)
    cfg.target = target
    cfg.has_target = lambda: bool(cfg.target)
    return cfg


def test_bare_word_becomes_the_target():
    cfg = _mk(['ReCpp'], None)          # `mama rebuild ReCpp`
    set_target_from_unused_args(cfg)
    assert cfg.target == 'ReCpp'


def test_option_shaped_typo_fails_immediately(capsys):
    for bad in ('jobz=4', '-buildstats'):
        cfg = _mk([bad], None)
        with pytest.raises(SystemExit):
            set_target_from_unused_args(cfg)
        assert f"unknown option '{bad}'" in capsys.readouterr().out
        assert cfg.target is None      # never silently reinterpreted as a target name


def test_unknown_target_error_lists_the_valid_ones(capsys):
    cfg = _mk([], 'buildstatz')
    cfg.targets_all = lambda: False
    root = Mock()
    with patch('mama.main.find_dependency', return_value=None), \
         patch('mama.main.get_flat_deps', return_value=[SimpleNamespace(name=n) for n in ('ReCpp', 'zlib')]):
        with pytest.raises(SystemExit):
            check_config_target(cfg, root)
    out = capsys.readouterr().out
    assert "target='buildstatz' not found" in out and 'ReCpp, zlib' in out


def test_a_retired_flag_names_its_replacement(capsys):
    # `buildtimes` would otherwise be read as a target name and fail with a confusing 'target not found'
    cfg = _mk(['buildtimes'], None)
    with pytest.raises(SystemExit):
        set_target_from_unused_args(cfg)
    out = capsys.readouterr().out
    assert "'buildtimes' was removed" in out and "'buildstats'" in out
    assert cfg.target is None
