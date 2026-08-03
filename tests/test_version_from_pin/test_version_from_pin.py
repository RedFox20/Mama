"""Pins P2: a git pin names the package. A tag names it alone, a branch names it alongside the commit,
and an unpinned dep still names itself by commit. Both sides read the same pin, so they cannot diverge."""
from unittest.mock import patch

import pytest

from mama import artifactory as art
from mama.build_names import sanitize_version
from mama.types.git import Git
from testutils import make_archive_name_target as _target

_HASH = 'df76b66'
_PREFIX = 'pkg-linux-24-gcc14-x64-release-'


def _name(**kw):
    """The archive name with the commit hash resolution stubbed, so no test touches the network."""
    with patch.object(Git, 'get_commit_hash', return_value=_HASH):
        return art.artifactory_archive_name(_target(version='', **kw))


@pytest.mark.parametrize('raw, safe', [
    ('1.0.0',                   '1.0.0'),      # the shape a version ideally has
    ('v0.1.3',                  'v0.1.3'),     # ...and the shapes real repos actually use
    ('n8.1.0',                  'n8.1.0'),
    ('8.0.1-lgpl',              '8.0.1-lgpl'),
    ('RELEASE_2_1',             'RELEASE_2_1'),
    ('release/1.0',             'release-1.0'),          # a slash is not a file name character
    ('feat/experimental-radio', 'feat-experimental-radio'),
    ('feature/JIRA-42_wip',     'feature-JIRA-42_wip'),
    ('v1.0 (rc1)',              'v1.0-rc1'),             # a run of unsafe characters collapses into one
    ('///',                     ''),                     # nothing safe left
    ('',                        ''),
])
def test_a_pin_becomes_a_file_name_without_losing_its_shape(raw, safe):
    # No parsing, no semver assumption, no stripped prefix: real tags differ too much for any of that.
    assert sanitize_version(raw) == safe


def test_the_case_and_the_v_prefix_survive():
    # Both look like noise to strip. Strip them and two different sources share one archive name:
    # lowercasing merges v1.0 with V1.0, and dropping the 'v' merges v1.0 with 1.0.
    assert len({sanitize_version(t) for t in ('v1.0', 'V1.0', '1.0')}) == 3


def test_a_tag_pin_names_the_package():
    assert _name(git_tag='v0.13.0') == _PREFIX + 'v0.13.0'


def test_a_tag_pin_never_resolves_a_commit_hash():
    # The composer names a tag-pinned package from the pin alone. This is NOT a network saving: the shim
    # probe resolves the hash first anyway, because the shim marker records it. It pins that the download
    # and the upload read the same string with no lookup between them.
    with patch.object(Git, 'get_commit_hash') as resolve:
        art.artifactory_archive_name(_target(version='', git_tag='v0.13.0'))
    resolve.assert_not_called()


def test_a_branch_pin_keeps_the_commit_and_reads_as_the_branch():
    # A branch MOVES. Its name alone would serve every commit ever pushed to it under one archive name,
    # so the hash stays and the branch only labels it.
    assert _name(git_branch='feat/experimental-radio') == _PREFIX + f'feat-experimental-radio-{_HASH}'


def test_a_branch_pin_still_produces_a_new_name_per_commit():
    with patch.object(Git, 'get_commit_hash', side_effect=['aaa1111', 'bbb2222']):
        first = art.artifactory_archive_name(_target(version='', git_branch='main'))
        second = art.artifactory_archive_name(_target(version='', git_branch='main'))
    assert first != second and first.endswith('main-aaa1111') and second.endswith('main-bbb2222')


def test_a_tag_wins_over_a_branch():
    # add_git can carry both. The tag is the immutable one, so it names the package.
    assert _name(git_tag='v0.13.0', git_branch='main') == _PREFIX + 'v0.13.0'


def test_an_unpinned_dep_still_names_itself_by_commit():
    assert _name(is_git=True) == _PREFIX + _HASH


def test_a_commit_pin_names_the_package_by_its_short_hash():
    # add_git stores git_commit in the tag field, so the tag rule would otherwise name the archive after
    # a 40 character hash. Git.is_hex_string is the same test the clone path uses to tell the two apart.
    assert _name(git_tag='4acd9052f27a459314651dd485ae8fa79a04d49d') == _PREFIX + _HASH


def test_a_mamafile_literal_beats_the_pin():
    # self.version is the dep's own statement about itself, and P1 made that statement trustworthy.
    with patch.object(Git, 'get_commit_hash', return_value=_HASH):
        name = art.artifactory_archive_name(_target(version='8.0.1', git_tag='v0.13.0'))
    assert name == _PREFIX + '8.0.1'


def test_the_variant_suffix_still_sits_before_the_version():
    assert _name(git_tag='v0.13.0', args=['LGPL'], sanitize='address') \
        == 'pkg-linux-24-gcc14-x64-release-asan-lgpl-v0.13.0'


def test_a_tag_survives_a_papa_txt_round_trip():
    # A child dep re-created from a package's papa.txt must name itself the same way, or the consumer
    # looks for one archive while the producer published another.
    git = Git('qcoro', 'https://github.com/qcoro/qcoro.git', '', 'v0.13.0', 'mamadeps/qcoro.py', True, [])
    back = Git.from_papa_string(git.get_papa_string()[len('git '):])
    assert back.tag == 'v0.13.0'
    assert sanitize_version(back.tag) == sanitize_version(git.tag)
