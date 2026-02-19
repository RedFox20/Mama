from testutils import init
from mama.papa_deploy import PapaFileInfo

# Test papa file format parsing
def test_papa_parse():
    init(__file__)

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
