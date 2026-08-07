"""Pins config.set_target_march: the pin replaces the platform -march, and it names the build."""
import pytest

from testutils import make_archive_name_target, make_mock_dep, platform_config, platform_cxx_flags
from mama import artifactory as art
from mama.build_names import arch_marker, build_dir_name, build_variant_suffix, is_build_dir_of, object_attributes
from mama.platforms.linux import Linux
from mama.platforms.windows import Windows


# --- the API refuses what cannot work ---

def test_an_unknown_arch_is_refused():
    with pytest.raises(RuntimeError, match='unknown arch'):
        platform_config(Linux).set_target_march('x86_64', 'x86-64-v3')


@pytest.mark.parametrize('march', ['-march=x86-64-v3', 'x86-64-v3 -mtune=generic'])
def test_the_value_must_be_the_march_alone(march):
    with pytest.raises(RuntimeError, match='must be the value alone'):
        platform_config(Linux).set_target_march('x64', march)


def test_an_empty_value_drops_the_pin():
    config = platform_config(Linux)
    config.set_target_march('x64', 'x86-64-v3')
    config.set_target_march('x64', '')
    assert config.target_march == {}


def test_a_platform_with_no_march_keeps_its_default(capsys):
    config = platform_config(Windows)
    config.set_target_march('x64', 'x86-64-v3')
    assert config.target_march == {} and 'has no -march' in capsys.readouterr().out


# --- the pin reaches the compiler, once ---

def test_the_pin_replaces_the_platform_march(tmp_path):
    flags = platform_cxx_flags(tmp_path, Linux, 'x64', target_march={'x64': 'x86-64-v3'})
    assert flags['-march'] == 'x86-64-v3'
    # a second -march only shadows the first, and compile_commands.json then reports the wrong one
    assert [f for f in flags if f.startswith('-march')] == ['-march']


def test_a_pin_for_another_arch_does_not_apply(tmp_path):
    flags = platform_cxx_flags(tmp_path, Linux, 'x64', target_march={'arm64': 'armv8.2-a'})
    assert flags['-march'] != 'armv8.2-a'


def test_a_platform_with_no_march_emits_none(tmp_path):
    assert '-march' not in platform_cxx_flags(tmp_path, Windows, 'x64', target_march={'x64': 'x86-64-v3'})


# --- the pin merges into the arch field of a name ---

@pytest.mark.parametrize('arch,march,marker', [
    ('x64',   'x86-64-v3', 'x64v3'),    # the arch already says x86-64, so the level alone is left
    ('x64',   'x86-64-v4', 'x64v4'),
    ('x64',   'x86-64',    'x64v1'),    # the psABI calls the bare baseline level 1
    ('arm64', 'armv8.2-a', 'armv82a'),
    ('x64',   'haswell',   'x64haswell'),  # a CPU name says no arch, so the marker puts one in front
    ('x86',   'pentium4',  'x86pentium4'),
    ('x64',   '',          'x64'),
])
def test_the_marker_merges_the_arch_and_the_pin(arch, march, marker):
    config = platform_config(Linux, arch)
    if march: config.set_target_march(arch, march)
    assert arch_marker(config) == marker


@pytest.mark.parametrize('march', ['x86-64', 'x86-64-v2', 'x86-64-v3', 'x86-64-v4'])
def test_no_x64_pin_ever_spells_the_unpinned_marker(march):
    # an unpinned x64 build compiles -march=native, so a pin that shared its name would hide two ABIs
    config = platform_config(Linux, 'x64')
    config.set_target_march('x64', march)
    assert arch_marker(config) != 'x64'


def test_the_pin_names_the_build_dir():
    config = platform_config(Linux, 'x64')
    config.set_target_march('x64', 'x86-64-v3')
    assert build_dir_name(config) == 'linux-x64v3'


def test_the_pin_names_the_archive():
    name = art.artifactory_archive_name(make_archive_name_target(march='x86-64-v3'))
    assert name == 'pkg-linux-24-gcc14-x64v3-release-abc1234'


def test_the_variant_keeps_only_its_own_axes():
    # the pin left the variant when it merged into the arch, so a sanitizer and a dep arg read as before
    target = make_archive_name_target(sanitize='address', march='x86-64-v3', args=['LGPL'])
    assert target.dep.variant_suffix == '-asan-lgpl'
    assert art.artifactory_archive_name(target) == 'pkg-linux-24-gcc14-x64v3-release-asan-lgpl-abc1234'


def test_the_papa_object_record_names_the_real_march():
    # the record is text, so it keeps the value a reader compares against a CPU, not the merged marker
    target = make_archive_name_target(march='x86-64-v3', sanitize='address', args=['LGPL'])
    assert object_attributes(target) == 'release linux x64 march=x86-64-v3 asan lgpl'


def test_two_pins_of_one_commit_produce_two_names():
    v2 = art.artifactory_archive_name(make_archive_name_target(march='x86-64-v2'))
    v3 = art.artifactory_archive_name(make_archive_name_target(march='x86-64-v3'))
    assert v2 != v3 and v2.endswith('-abc1234') and v3.endswith('-abc1234')


def test_an_unpinned_build_keeps_its_old_name():
    config = platform_config(Linux, 'x64')
    assert build_variant_suffix(config) == '' and build_dir_name(config) == 'linux'


@pytest.mark.parametrize('march,dir_name', [('x86-64-v3', 'linux-x64v3'), ('haswell', 'linux-x64haswell')])
def test_a_baseline_clean_leaves_a_pinned_tree(march, dir_name):
    config = platform_config(Linux, 'x64')
    config.set_target_march('x64', march)
    assert build_dir_name(config) == dir_name
    assert is_build_dir_of(dir_name, 'linux') is False  # the unpinned run must not sweep it away


def test_the_dirs_re_resolve_after_the_root_pins_the_march(tmp_path):
    # build_dir is computed at BuildTarget construction, before settings() reaches set_target_march
    dep = make_mock_dep(tmp_path)
    dep._update_dep_name_and_dirs(dep.name)
    assert dep.build_dir.endswith('/linux')
    dep.config.target_march = {'x64': 'x86-64-v3'}
    dep._update_dep_name_and_dirs(dep.name)
    assert dep.build_dir.endswith('/linux-x64v3')
