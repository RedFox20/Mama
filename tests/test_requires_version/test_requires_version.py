"""Pins mamafile `self.requires_version(...)`: numeric-segment comparison (0.13 outranks 0.9) and an
abort carrying the pip upgrade hint when the running mamabuild is too old."""
import pytest

import mama.build_target as build_target
from mama.build_target import BuildTarget
from mama.util import parse_version, version_at_least


def _target(name='myapp'):
    t = object.__new__(BuildTarget)  # skip the heavy __init__; requires_version only needs .name
    t.name = name
    return t


@pytest.mark.parametrize('text,expected', [
    ('0.13.01', (0, 13, 1)),   # zero-padded patch parses numerically
    ('0.9.5', (0, 9, 5)),
    ('1', (1,)),
    ('2.0.0-rc1', (2, 0, 1)),  # non-numeric junk in a segment is dropped
])
def test_parse_version(text, expected):
    assert parse_version(text) == expected


@pytest.mark.parametrize('current,required,ok', [
    ('0.13.01', '0.9.5', True),    # the trap: a plain string compare ranks '0.13' BELOW '0.9'
    ('0.13.01', '0.13.01', True),  # equal satisfies the requirement
    ('0.13.02', '0.13.01', True),
    ('0.12.0', '0.13.01', False),
    ('0.13', '0.13.01', False),    # shorter side zero-pads -> 0.13.0 < 0.13.01
])
def test_version_at_least(current, required, ok):
    assert version_at_least(current, required) is ok


def test_requires_version_passes_when_current_is_new_enough(monkeypatch):
    monkeypatch.setattr(build_target, '__version__', '0.13.01')
    _target().requires_version('0.13.01')   # equal, and no raise
    _target().requires_version('0.9.5')


def test_requires_version_aborts_with_pip_upgrade_hint(monkeypatch):
    monkeypatch.setattr(build_target, '__version__', '0.12.5')
    with pytest.raises(RuntimeError) as e:
        _target().requires_version('0.13.01')
    msg = str(e.value)
    assert 'myapp' in msg and '0.13.01' in msg and '0.12.5' in msg  # names target, wanted, actual
    assert 'pip install --upgrade mama' in msg                          # tells the user how to fix it
