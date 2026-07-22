"""Pins the papa.txt compiler stamp check: warn on a foreign-compiler package, stay quiet otherwise."""
from unittest.mock import Mock, patch
from mama import artifactory


def _mismatch_warnings(papa_compiler, current='gcc14.3'):
    target = Mock(); target.name = 'libfoo'
    target.config.compiler_version.return_value = current
    warnings = []
    with patch('mama.artifactory.warning', side_effect=warnings.append):
        artifactory._warn_on_compiler_mismatch(target, Mock(compiler=papa_compiler))
    return warnings


def test_a_package_built_by_another_compiler_warns():
    assert 'clang18.1' in _mismatch_warnings('clang18.1')[0]


def test_matching_or_unstamped_packages_stay_quiet():
    assert not _mismatch_warnings('gcc14.3')
    assert not _mismatch_warnings(None)  # pre-change package: unknown, allow it
