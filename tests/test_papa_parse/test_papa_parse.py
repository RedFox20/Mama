import os
from mama.papa_deploy import PapaFileInfo

# Tests new papa file format parsing
def test_papa_parse():
    os.chdir(os.path.dirname(os.path.abspath(__file__)))

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
    assert dep.commit == '4acd9052f27a459314651dd485ae8fa79a04d49d'

    assert len(papa.includes) == 2
    assert papa.includes[0].endswith('include')
    assert papa.includes[1].endswith('include/test_papa_deploy')

    assert len(papa.libs) == 1
    assert papa.libs[0].endswith('RelWithDebInfo/ExampleConsumer.lib')

    assert len(papa.syslibs) == 0
    assert len(papa.assets) == 0

# Test old papa file format parsing
def test_papa_parse_old():
    os.chdir(os.path.dirname(os.path.abspath(__file__)))

    papa = PapaFileInfo('papa_old.txt')

    assert papa.project_name == 'ExampleConsumer'

    assert len(papa.dependencies) == 1
    dep = papa.dependencies[0]
    assert dep.is_git
    assert dep.name == 'ExampleRemote'
    assert dep.url == 'https://github.com/BatteredBunny/MamaExampleRemote.git'
    assert dep.branch == ''
    assert dep.tag == ''
    assert dep.mamafile == ''
    assert dep.commit == ''

    assert len(papa.includes) == 2
    assert papa.includes[0].endswith('include')
    assert papa.includes[1].endswith('include/test_papa_deploy')

    assert len(papa.libs) == 1
    assert papa.libs[0].endswith('RelWithDebInfo/ExampleConsumer.lib')

    assert len(papa.syslibs) == 0
    assert len(papa.assets) == 0