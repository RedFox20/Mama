"""Pins the automatic module export: a recipe that names none gets every module its includes hold."""
import os

import pytest
from testutils import make_package_target, write_files

from mama import package as package_mod

CPPM = 'export module rpp.strview;\n'
FILES = {'include/rpp/strview.h': '#pragma once\n', 'include/rpp/rpp-strview.cppm': CPPM,
         'include/rpp/rpp-vec.cppm': CPPM, 'src/rpp/private.cppm': CPPM}
BOTH = ['rpp-strview.cppm', 'rpp-vec.cppm']


def _include_hook(self): self.export_include('include')


def _run(tmp_path, package, fetched=False, exports=None):
    """Run the packaging of a target whose package() hook is `package`. Returns the target."""
    target = make_package_target(tmp_path, package=package, exports=exports,
                                 dep_attrs={'from_artifactory': fetched, 'should_rebuild': True})
    write_files(target.source_dir(), FILES)
    target._run_packaging()
    assert target.exported_includes, 'the packaging never ran, so every assert below passes for free'
    return target


def _names(modules) -> list:
    return sorted(os.path.basename(m) for m in modules)


def test_the_packaging_exports_every_module_an_exported_include_holds(tmp_path):
    # src/rpp/private.cppm is not under the exported dir, and a module that cannot deploy stays out
    assert _names(_run(tmp_path, _include_hook).exported_modules) == BOTH


@pytest.mark.parametrize('hook, expected', [
    # an explicit export narrows the automatic one
    (lambda self: (self.export_include('include'),
                   self.export_modules('include/rpp', ['rpp-strview.cppm'])), ['rpp-strview.cppm']),
    # a recipe may collect the rest of the defaults after it named the modules it wants
    (lambda self: (self.export_include('include'),
                   self.export_modules('include/rpp', ['rpp-strview.cppm']),
                   self.default_package()), ['rpp-strview.cppm']),
    # a recipe may name a module only some platforms build, and it means the empty result
    (lambda self: (self.export_include('include'),
                   self.export_modules('include/rpp', ['not-on-this-platform.cppm'])), []),
    # the opt-out
    (lambda self: (self.no_export_modules(), self.export_include('include')), []),
])
def test_a_call_to_export_modules_decides_whatever_it_resolves_to(tmp_path, hook, expected):
    assert _names(_run(tmp_path, hook).exported_modules) == expected


def test_a_fetched_package_honors_no_export_modules(tmp_path):
    # papa.txt owns the list for a category the hook left alone, and this hook did not leave it alone
    hook = lambda self: (self.no_export_modules(), self.export_include('include'))
    target = _run(tmp_path, hook, fetched=True, exports=([], [], [], [], ['old.cppm']))
    assert target.exported_modules == []


def test_a_fetched_hook_that_re_roots_the_includes_finds_its_modules_again(tmp_path):
    # the archive records deployed module paths, and a hook that exports the source tree re-roots them
    deployed = f'{tmp_path}/deploy/include/rpp/old.cppm'
    target = _run(tmp_path, _include_hook, fetched=True,
                  exports=([f'{tmp_path}/deploy/include'], [], [], [], [deployed]))
    assert _names(package_mod.exported_modules_with_base(target)) == BOTH
