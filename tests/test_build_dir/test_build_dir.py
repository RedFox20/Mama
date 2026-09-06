"""Pins build dir names: sanitizer, coverage, compiler and dep-args suffixes."""
import pytest
from mama.build_names import build_dir_name
from testutils import linux_config, platform_config
from mama.platforms.macos import Macos
from mama.platforms.ios import Ios
from mama.platforms.android import Android
from mama.platforms.windows import Windows
from mama.platforms.oclea import Oclea


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
    # the dir and the archive must not spell one axis two ways ('linux-memory' vs 'msan')
    c = linux_config()
    c.sanitize = 'memory'
    assert build_dir_name(c) == 'linux-msan'


def _dep_with_args(tmp_path, args, name='libffmpeg', is_root=True, **cfg_overrides):
    """A real BuildDependency on a real BuildConfig. __init__ composes the variant suffix and the dirs
    without any clone or disk write, so the name is known before the clone."""
    from mama.build_dependency import BuildDependency
    from mama.types.git import Git
    cfg = linux_config()
    cfg.workspaces_root = str(tmp_path)
    for k, v in cfg_overrides.items(): setattr(cfg, k, v)
    git = Git(name=name, url=f'https://example.com/{name}.git', branch='', tag='',
              mamafile=None, shallow=True, args=args)
    dep = BuildDependency(parent=None, config=cfg, workspace='packages', dep_source=git)
    if not is_root:
        dep.is_root = False
        dep._update_dep_name_and_dirs(dep.name)  # is_root picks the coverage variant
    return dep


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


# --- coverage scopes to the target the user named, the sanitizer stays tree-wide ---

def _cov_dir(tmp_path, name='libffmpeg', is_root=False, **cfg):
    return _dep_with_args(tmp_path, [], name=name, is_root=is_root, coverage='default', **cfg).build_dir_name


def test_a_coverage_run_with_no_target_instruments_the_root_alone(tmp_path):
    assert _cov_dir(tmp_path, is_root=True) == 'linux-cov'
    assert _cov_dir(tmp_path) == 'linux'


def test_a_dep_outside_the_coverage_target_keeps_the_dir_it_uses_without_coverage(tmp_path):
    assert _cov_dir(tmp_path, name='libffmpeg', user_target='libffmpeg') == 'linux-cov'
    assert _cov_dir(tmp_path, name='libnet', user_target='libffmpeg') == 'linux'
    assert _cov_dir(tmp_path, name='libnet', user_target='libffmpeg', sanitize='address') == 'linux-asan'


def test_the_target_all_instruments_every_dep(tmp_path):
    assert _cov_dir(tmp_path, name='libnet', user_target='all') == 'linux-cov'
    assert _cov_dir(tmp_path, name='libnet', user_target='all', sanitize='address') == 'linux-cov-asan'


def test_a_command_that_rewrites_the_target_does_not_widen_the_coverage(tmp_path):
    # `update` and `deps_only` rewrite config.target to 'all', so the predicate reads user_target
    assert _cov_dir(tmp_path, name='libnet', target='all') == 'linux'
    assert _cov_dir(tmp_path, is_root=True, target='all') == 'linux-cov'
