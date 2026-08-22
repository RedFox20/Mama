"""Pins the automatic module export: a recipe that names none gets every module its includes hold."""
import os

from testutils import make_package_target, write_files

CPPM = 'export module rpp.strview;\n'
FILES = {'include/rpp/strview.h': '#pragma once\n', 'include/rpp/rpp-strview.cppm': CPPM,
         'include/rpp/rpp-vec.cppm': CPPM, 'src/rpp/private.cppm': CPPM}


def _packaged(tmp_path, package) -> list:
    """Run the packaging of a target whose package() hook is `package`. Returns its module names."""
    target = make_package_target(tmp_path, package=package,
                                 dep_attrs={'from_artifactory': False, 'should_rebuild': True})
    write_files(target.source_dir(), FILES)
    target._run_packaging()
    assert target.exported_includes, 'the packaging never ran, so every assert below passes for free'
    return sorted(os.path.basename(m) for m in target.exported_modules)


def test_the_packaging_exports_every_module_an_exported_include_holds(tmp_path):
    # src/rpp/private.cppm is not under the exported dir, and a module that cannot deploy stays out
    def package(self): self.export_include('include')
    assert _packaged(tmp_path, package) == ['rpp-strview.cppm', 'rpp-vec.cppm']


def test_an_explicit_export_narrows_the_automatic_one(tmp_path):
    def package(self):
        self.export_include('include')
        self.export_modules('include/rpp', ['rpp-strview.cppm'])
    assert _packaged(tmp_path, package) == ['rpp-strview.cppm']


def test_default_package_never_widens_a_narrowed_list(tmp_path):
    # a recipe may collect the rest of the defaults after it named the modules it wants
    def package(self):
        self.export_include('include')
        self.export_modules('include/rpp', ['rpp-strview.cppm'])
        self.default_package()
    assert _packaged(tmp_path, package) == ['rpp-strview.cppm']


def test_an_export_that_resolves_to_nothing_is_still_a_declaration(tmp_path):
    # a recipe may name a module only some platforms build, and it means the empty result
    def package(self):
        self.export_include('include')
        self.export_modules('include/rpp', ['not-on-this-platform.cppm'])
    assert _packaged(tmp_path, package) == []


def test_a_fetched_package_honors_no_export_modules(tmp_path):
    # papa.txt owns the list for a category the hook left alone, and this hook did not leave it alone
    def package(self):
        self.no_export_modules()
        self.export_include('include')
    target = make_package_target(tmp_path, package=package, exports=([], [], [], [], ['old.cppm']),
                                 dep_attrs={'from_artifactory': True, 'should_rebuild': True})
    write_files(target.source_dir(), FILES)
    target._run_packaging()
    assert target.exported_modules == []


def test_no_export_modules_opts_out(tmp_path):
    def package(self):
        self.no_export_modules()
        self.export_include('include')
    assert _packaged(tmp_path, package) == []
