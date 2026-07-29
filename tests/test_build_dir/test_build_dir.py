import pytest
from mama.build_names import build_dir_name
from testutils import platform_config
from mama.platforms.linux import Linux
from mama.platforms.macos import Macos
from mama.platforms.ios import Ios
from mama.platforms.android import Android
from mama.platforms.windows import Windows
from mama.platforms.oclea import Oclea


def linux_config():
    """A BuildConfig pinned to linux/x64 so dir names are host-independent."""
    return platform_config(Linux, 'x64')


def test_no_sanitizer_dir_unchanged():
    c = linux_config()
    assert build_dir_name(c, platform_dir='linux') == 'linux'
    assert build_dir_name(c) == 'linux'


def test_each_sanitizer_gets_own_dir():
    c = linux_config()
    for sanitize, expected in [('address', 'linux-asan'),
                               ('thread',  'linux-tsan'),
                               ('undefined', 'linux-ubsan'),
                               ('leak',    'linux-lsan')]:
        c.sanitize = sanitize
        assert build_dir_name(c) == expected


def test_combined_sanitizers_stay_distinct():
    c = linux_config()
    c.sanitize = 'address,undefined'
    assert build_dir_name(c) == 'linux-asan-ubsan'


def test_coverage_gets_own_dir():
    c = linux_config()
    c.coverage = 'default'
    assert build_dir_name(c) == 'linux-cov'


def test_coverage_composes_with_sanitizer():
    c = linux_config()
    c.coverage = 'default'
    c.sanitize = 'address'
    assert build_dir_name(c) == 'linux-cov-asan'


def test_clang_gets_its_own_dir_and_gcc_keeps_the_bare_name():
    c = linux_config()
    assert build_dir_name(c) == 'linux'  # gcc default: no churn for existing trees
    c.clang = True; c.gcc = False
    assert build_dir_name(c) == 'linux-clang'


def test_compiler_is_the_coarsest_suffix():
    c = linux_config()
    c.clang = True; c.sanitize = 'thread'
    assert build_dir_name(c) == 'linux-clang-tsan'
    c.coverage = 'default'; c.sanitize = 'address'
    assert build_dir_name(c) == 'linux-clang-cov-asan'


def test_arm_linux_also_gets_the_clang_suffix():
    c = linux_config()
    c.arch = 'arm64'; c.clang = True
    assert build_dir_name(c) == 'linuxarm-clang'


@pytest.mark.parametrize('platform_class', [Macos, Ios, Android, Windows, Oclea])
def test_non_linux_platforms_are_unaffected_by_clang(platform_class):
    # set_platform() is exclusive: these never see the suffix, toolset/SDK fixes their compiler
    c = platform_config(platform_class, clang=True)
    assert '-clang' not in build_dir_name(c)


def test_a_yocto_board_is_named_by_its_own_build_dir():
    assert build_dir_name(platform_config(Oclea)) == 'oclea'


def test_msan_uses_the_same_short_name_as_the_archive():
    # The dir used to say 'linux-memory' while the archive said 'msan': two tables, one axis.
    c = linux_config()
    c.sanitize = 'memory'
    assert build_dir_name(c) == 'linux-msan'


def _dep_with_args(tmp_path, args, **cfg_overrides):
    """A real BuildDependency on a real BuildConfig. __init__ composes the variant suffix and the dirs
    without any clone or disk write, which is the whole point: the name is known before the clone."""
    from mama.build_dependency import BuildDependency
    from mama.types.git import Git
    cfg = linux_config()
    cfg.workspaces_root = str(tmp_path)
    for k, v in cfg_overrides.items(): setattr(cfg, k, v)
    git = Git(name='libffmpeg', url='https://example.com/libffmpeg.git', branch='', tag='',
              mamafile=None, shallow=True, args=args)
    return BuildDependency(parent=None, config=cfg, workspace='packages', dep_source=git)


def test_dep_args_get_their_own_build_dir(tmp_path):
    assert _dep_with_args(tmp_path, ['LGPL']).build_dir.endswith('/linux-lgpl')
    assert _dep_with_args(tmp_path, []).build_dir.endswith('/linux')  # no args: the old dir


def test_the_dep_build_dir_is_built_from_the_one_variant_suffix(tmp_path):
    # The same string the archive name carries, so a build and its package cannot disagree.
    dep = _dep_with_args(tmp_path, ['LGPL'], sanitize='address', coverage='default')
    assert dep.variant_suffix == '-cov-asan-lgpl'
    assert dep.build_dir_name == 'linux-cov-asan-lgpl'  # stored once, read by every consumer
    assert dep.build_dir.endswith('/' + dep.build_dir_name)


def test_a_second_parent_with_more_args_updates_the_build_dir(tmp_path):
    # add_child dedups by name and unions the args, so the dir has to follow the union.
    from mama.types.git import Git
    dep = _dep_with_args(tmp_path, ['LGPL'])
    dep.update_existing_dependency(Git(name='libffmpeg', url='https://example.com/libffmpeg.git',
                                       branch='', tag='', mamafile=None, shallow=True, args=['NEWMATH=1']))
    assert dep.variant_suffix == '-lgpl-newmath1'
    assert dep.build_dir.endswith('/linux-lgpl-newmath1')
