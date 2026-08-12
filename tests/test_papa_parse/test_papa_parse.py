"""Pins PapaFileInfo: parsing every record kind of papa.txt, including the optional compiler record."""
from testutils import init
from mama.papa_deploy import PapaFileInfo


def test_papa_parse(tmp_path):
    init(__file__, tmp_path)

    papa = PapaFileInfo('papa.txt')

    assert papa.project_name == 'ExampleConsumer'

    assert len(papa.dependencies) == 1
    dep = papa.dependencies[0]
    assert dep.is_git
    assert dep.name == 'ExampleRemote'
    assert dep.url == 'https://github.com/BatteredBunny/MamaExampleRemote.git'
    assert dep.branch == ''
    assert dep.tag == ''
    assert dep.mamafile == ''

    assert len(papa.includes) == 2
    assert papa.includes[0].endswith('include')
    assert papa.includes[1].endswith('include/test_papa_deploy')

    assert len(papa.libs) == 1
    assert papa.libs[0].endswith('RelWithDebInfo/ExampleConsumer.lib')

    assert len(papa.syslibs) == 0
    assert len(papa.assets) == 0


def test_compiler_record_round_trips(tmp_path):
    papa = tmp_path / 'papa.txt'
    papa.write_text('P Example\nC gcc14.3\nI include\n')
    assert PapaFileInfo(str(papa)).compiler == 'gcc14.3'


def test_a_package_without_a_compiler_record_still_loads(tmp_path):
    # older packages have no C record: unknown must not read as mismatch
    papa = tmp_path / 'papa.txt'
    papa.write_text('P Example\nI include\n')
    info = PapaFileInfo(str(papa))
    assert info.compiler is None and info.project_name == 'Example'


def test_a_module_record_resolves_against_the_package_dir(tmp_path):
    papa = tmp_path / 'papa.txt'
    papa.write_text('P Example\nI include\nM include/rpp/rpp-strview.cppm\n')
    assert PapaFileInfo(str(papa)).modules == [f'{tmp_path}/include/rpp/rpp-strview.cppm']


def test_an_unknown_record_kind_parses_as_nothing(tmp_path):
    # a new record must never break an older mama, so the parse loop skips what it does not know
    papa = tmp_path / 'papa.txt'
    papa.write_text('P Example\nZ something new\nI include\n')
    info = PapaFileInfo(str(papa))
    assert info.project_name == 'Example' and info.modules == [] and len(info.includes) == 1
