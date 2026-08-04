"""Dep args in the build dir name and the archive name: one repo, one commit, two builds that must not
overwrite each other (libffmpeg ships GPL to one consumer and LGPLv3 to another)."""
import pytest

from mama import artifactory as art, build_names
from testutils import make_archive_name_target as _target, plain_config

_NO_ARGS = 'pkg-linux-24-gcc14-x64-release-abc1234'


@pytest.mark.parametrize('args, suffix', [
    ((),                       ''),
    (['lgpl'],                 '-lgpl'),
    (['LGPL'],                 '-lgpl'),            # the case never reaches a name
    (['NEWMATH=1'],            '-newmath1'),        # key=value keeps both halves
    (['NEWMATH=2'],            '-newmath2'),        # ...so two values stay distinct
    (['C++20'],                '-cpp20'),           # '+' reads as 'p', the way a reader writes it
    (['b', 'a'],               '-a-b'),             # sorted: the call order must not change a name
    (['a', 'b'],               '-a-b'),
    (['GPL', 'gpl'],           '-gpl'),             # de-duplicated after normalizing
    (['LGPL', 'NEWMATH=1'],    '-lgpl-newmath1'),
    (['--', ''],               ''),                 # nothing left after normalizing: no token
    (['naïve', 'Ünïcøde'],     '-nave-ncde'),       # a file name on an FTP server stays ASCII
])
def test_the_suffix_normalizes_every_arg_shape(args, suffix):
    assert build_names.build_variant_suffix(plain_config(), args) == suffix


def test_a_dep_without_args_keeps_its_old_name():
    # No-args names predate this field: they must stay byte-identical or every published archive cache misses at once.
    assert art.artifactory_archive_name(_target()) == _NO_ARGS


def test_the_args_sit_before_the_version():
    assert art.artifactory_archive_name(_target(args=['lgpl'])) == 'pkg-linux-24-gcc14-x64-release-lgpl-abc1234'


def test_two_arg_sets_of_one_commit_produce_two_names():
    gpl = art.artifactory_archive_name(_target(args=['gpl']))
    lgpl = art.artifactory_archive_name(_target(args=['lgpl']))
    assert gpl != lgpl and gpl.endswith('-abc1234') and lgpl.endswith('-abc1234')  # same commit


def test_the_call_order_of_the_args_does_not_change_the_name():
    assert art.artifactory_archive_name(_target(args=['b', 'a'])) \
        == art.artifactory_archive_name(_target(args=['a', 'b']))


def test_a_repeated_arg_does_not_change_the_name():
    # BuildDependency._set_args appends, so a dep added by two parents can carry the same arg twice.
    assert art.artifactory_archive_name(_target(args=['lgpl', 'lgpl'])) \
        == art.artifactory_archive_name(_target(args=['lgpl']))


def test_every_arg_gets_its_own_field():
    name = art.artifactory_archive_name(_target(args=['LGPL', 'NEWMATH=1']))
    assert name == 'pkg-linux-24-gcc14-x64-release-lgpl-newmath1-abc1234'


def test_a_sanitizer_and_the_args_both_appear_coarsest_first():
    name = art.artifactory_archive_name(_target(sanitize='address', args=['LGPL']))
    assert name == 'pkg-linux-24-gcc14-x64-release-asan-lgpl-abc1234'


def test_the_archive_name_carries_the_dep_variant_suffix_verbatim():
    # The dep computes one suffix at init. The build dir and the archive name use that exact string, so they cannot disagree.
    target = _target(sanitize='address', coverage='default', args=['LGPL', 'NEWMATH=1'])
    assert target.dep.variant_suffix == '-cov-asan-lgpl-newmath1'
    assert f'-release{target.dep.variant_suffix}-abc1234' in art.artifactory_archive_name(target)
