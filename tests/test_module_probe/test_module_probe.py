"""Pins that a CI job which pinned MAMA_TEST_COMPILER exercises that toolchain, or fails loudly."""
import contextlib
import os
import shutil
from unittest.mock import patch

import pytest
import testutils


@pytest.fixture(autouse=True)
def _fresh_probe():
    testutils._probe_module_compiler.cache_clear()
    yield
    testutils._probe_module_compiler.cache_clear()


@contextlib.contextmanager
def _stubbed_host(tools=None):
    """Answer every host probe from `tools`, a name to path map. A tool it does not name is absent.
    The probe runs cmake, ninja and the compiler, and no unit test here may read the real ones."""
    def piped(cmd, **kwargs): return 'cmake version 3.28.3' if 'cmake' in cmd[0] else '18.1.3'
    with patch.object(testutils, 'execute_piped', piped), \
         patch.object(shutil, 'which', lambda name, path=None: (tools or {}).get(name)):
        yield


CLANG_HOST = {'ninja': '/usr/bin/ninja', 'clang++': '/usr/bin/clang++', 'clang': '/usr/bin/clang'}


def test_a_pinned_msvc_job_selects_msvc(monkeypatch):
    monkeypatch.setenv('MAMA_TEST_COMPILER', 'msvc')
    assert testutils.module_compilers() == (('msvc', '', '', 0),)
    with _stubbed_host(), patch.object(testutils, 'is_windows', return_value=True):
        assert testutils.module_capable_compiler()['name'] == 'msvc'


def test_a_pin_no_host_can_answer_fails_instead_of_skipping(monkeypatch):
    # a skipped cell reports green, so a broken module path would reach a release unseen
    monkeypatch.setenv('MAMA_TEST_COMPILER', 'msvc')
    with _stubbed_host(), patch.object(testutils, 'is_windows', return_value=False):
        with pytest.raises(RuntimeError, match='MAMA_TEST_COMPILER=msvc'):
            testutils.module_capable_compiler()


def test_an_unpinned_host_still_skips(monkeypatch):
    monkeypatch.delenv('MAMA_TEST_COMPILER', raising=False)
    with patch.object(testutils, '_probe_module_compiler', return_value={}):
        assert testutils.module_capable_compiler() == {}


def test_a_clang_with_the_scanner_is_module_capable(monkeypatch):
    # the pair below only means something if the same host reaches the scanner test and passes it
    monkeypatch.setenv('MAMA_TEST_COMPILER', 'clang')
    with _stubbed_host(CLANG_HOST), patch.object(testutils, 'clang_scan_deps', return_value='/usr/bin/cs'):
        assert testutils.module_capable_compiler()['name'] == 'clang'


def test_a_clang_without_the_scanner_is_not_module_capable(monkeypatch):
    # cmake turns modules off without clang-scan-deps, and the test then fails instead of skipping
    monkeypatch.setenv('MAMA_TEST_COMPILER', 'clang')
    with _stubbed_host(CLANG_HOST), patch.object(testutils, 'clang_scan_deps', return_value=''):
        with pytest.raises(RuntimeError, match='MAMA_TEST_COMPILER=clang'):
            testutils.module_capable_compiler()


def test_the_scanner_search_reads_the_dir_of_the_real_compiler(tmp_path):
    # a distro symlinks clang++ into its llvm dir, and the scanner sits there under no suffix
    llvm = tmp_path / 'llvm' / 'bin'
    llvm.mkdir(parents=True)
    # shutil.which reads PATHEXT, so a name without it is invisible on Windows
    scanner = llvm / ('clang-scan-deps.exe' if testutils.is_windows() else 'clang-scan-deps')
    scanner.write_text('#!/bin/sh\n')
    scanner.chmod(0o755)
    link = tmp_path / 'clang++'
    try: link.symlink_to(llvm / 'clang++-18')
    except OSError: pytest.skip('this host refuses to create a symlink')
    # which builds the name from PATHEXT, which spells the extension in caps, so normcase compares them
    assert os.path.normcase(testutils.clang_scan_deps(str(link))) == os.path.normcase(str(scanner))


def test_a_host_with_no_scanner_anywhere_answers_empty(tmp_path):
    with patch.object(shutil, 'which', return_value=None):
        assert testutils.clang_scan_deps(str(tmp_path / 'clang++')) == ''
