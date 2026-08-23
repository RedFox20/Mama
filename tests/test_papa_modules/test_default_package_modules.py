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
def _include_hook_none(self): pass  # a recipe from before export_modules existed


def _run(tmp_path, package, fetched=False, exports=None, files=None):
    """Run the packaging of a target whose package() hook is `package`. Returns the target."""
    attrs = {'from_artifactory': fetched, 'should_rebuild': True}
    if files == {}: attrs['is_artifactory_shim'] = lambda: True  # no checkout, the shim shape
    target = make_package_target(tmp_path, package=package, exports=exports, dep_attrs=attrs)
    write_files(target.source_dir(), FILES if files is None else files)
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


def test_a_fetched_hook_that_narrows_modules_without_includes_keeps_the_archived_ones(tmp_path):
    # the hook names its modules in the source tree, and an include category it left alone still names
    # the deployed one. The two shared no base, so the fetched package exported no module at all.
    deployed = f'{tmp_path}/deploy/include'
    hook = lambda self: self.export_modules('include/rpp', ['rpp-strview.cppm'])
    target = _run(tmp_path, hook, fetched=True,
                  exports=([deployed], [], [], [], [f'{deployed}/rpp/rpp-strview.cppm',
                                                    f'{deployed}/rpp/rpp-vec.cppm']))
    assert _names(package_mod.exported_modules_with_base(target)) == ['rpp-strview.cppm']


def test_a_fetched_package_with_no_m_records_exports_no_module(tmp_path):
    # an empty M list is the publisher saying this package ships none. Rediscovery exported a .cppm
    # the include filter merely carried, so a consumer compiled a module the archive never declared.
    deployed = f'{tmp_path}/deploy/include'
    write_files(f'{tmp_path}/deploy', {'include/rpp/carried.cppm': CPPM, 'include/rpp/strview.h': '#pragma once\n'})
    target = _run(tmp_path, _include_hook_none, fetched=True, exports=([deployed], [], [], [], []))
    assert target.exported_modules == []


def test_a_fetched_narrowing_hook_tells_two_modules_of_one_name_apart(tmp_path):
    # a/api.cppm and b/api.cppm share a file name, and a file-name match restored both, so the
    # package shipped the very module the recipe excluded
    deployed = f'{tmp_path}/deploy/include'
    hook = lambda self: self.export_modules('include/a', ['api.cppm'])
    target = _run(tmp_path, hook, fetched=True, files={'include/a/api.cppm': CPPM, 'include/b/api.cppm': CPPM},
                  exports=([deployed], [], [], [], [f'{deployed}/a/api.cppm', f'{deployed}/b/api.cppm']))
    assert target.exported_modules == [f'{deployed}/a/api.cppm']


def test_a_shim_keeps_the_archived_modules_its_recipe_could_not_resolve(tmp_path):
    # a shim has no checkout, so export_modules() resolves nothing and still sets modules_declared.
    # Reading that empty result as the answer dropped every module the archive recorded.
    deployed = f'{tmp_path}/deploy/include'
    hook = lambda self: (self.export_include('include'),
                         self.export_modules('include/rpp', ['rpp-strview.cppm']))
    target = _run(tmp_path, hook, fetched=True, files={},  # no source tree: that is what a shim is
                  exports=([deployed], [], [], [], [f'{deployed}/rpp/rpp-strview.cppm']))
    assert target.exported_modules == [f'{deployed}/rpp/rpp-strview.cppm']
