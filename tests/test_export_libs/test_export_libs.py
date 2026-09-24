"""Pins which copy of a lib export_libs keeps when a build dir holds several copies of one basename."""
from testutils import make_stub_target, touch_file

from mama import package


def _export_libs(tmp_path, *libs):
    target = make_stub_target(tmp_path)
    target.cmake_build_type, target.exported_libs = 'RelWithDebInfo', []
    for lib in libs: touch_file(target.build_dir(lib))
    package.export_libs(target, '.', ['.lib'], build_dir=True, order=None)
    prefix = len(target.build_dir()) + 1
    return [lib[prefix:] for lib in target.exported_libs]


def test_the_lib_of_the_build_type_wins_over_a_stale_root_lib_and_a_debug_lib(tmp_path):
    # the root copy is what plain Ninja left before a switch to Ninja Multi-Config
    assert _export_libs(tmp_path, 'app.lib', 'Debug/app.lib', 'RelWithDebInfo/app.lib') == ['RelWithDebInfo/app.lib']


def test_a_single_config_dir_keeps_the_lib_in_its_root(tmp_path):
    assert _export_libs(tmp_path, 'app.lib', 'sub/util.lib') == ['app.lib', 'sub/util.lib']
